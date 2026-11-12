"""Download statistics.

The tracking endpoint records one event per request for an episode's audio.
Events are kept raw and counted when read, so the counting rules can change
without losing history.

Two backends sit behind ``StatsStore``:

* ``BigQueryStats``: production. ``record`` writes a structured log line; a
  Cloud Logging sink forwards those into a BigQuery table, which ``rows``
  queries. The app never inserts rows itself, so BigQuery can't slow down or
  break a download.
* ``LocalStats``: development and tests. Events go to a JSON-lines file.

Both return the same de-duplicated rows, and everything after that (bot
filtering, totals, the per-app breakdown) is shared code in ``summarize``.

Counting rules, roughly following the IAB podcast guidelines:

* ``GET`` requests only.
* Ignore range requests that don't start at byte 0, and the 2-byte
  ``bytes=0-1`` probe some apps send first.
* One download per listener, app and episode per UTC day.
* Known bots and crawlers are left out.

Listeners are told apart by a salted hash of their IP address. The raw
address is never stored.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .storage import Conflict, Storage

EVENT_NAME = "bucketcast.download"
SALT_PATH = "config/stats-salt.json"
BQ_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_$]+$")

BOT_RE = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|scrap|monitor|uptime|pingdom|headless|preview|validator"
    r"|^curl/|^wget/|python-requests|python-urllib|aiohttp|go-http-client|libwww"
    r"|facebookexternalhit|feedfetcher",
    re.IGNORECASE,
)

# First match wins, so more specific names come first.
APP_PATTERNS = [
    ("Apple Podcasts", r"applecoremedia|^podcasts/|apple ?podcasts|itunes|itms"),
    ("Spotify", r"spotify"),
    ("Overcast", r"overcast"),
    ("Pocket Casts", r"pocket ?casts|shifty jelly"),
    ("Castro", r"castro"),
    ("Podcast Addict", r"podcast ?addict"),
    ("AntennaPod", r"antennapod"),
    ("Castbox", r"castbox"),
    ("YouTube Music", r"youtube|google-podcast|googlechirp"),
    ("Amazon Music / Audible", r"amazon ?music|audible|alexa"),
    ("Web browser", r"mozilla/|chrome/|safari/|firefox/|edg/"),
]
_APP_RES = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in APP_PATTERNS]


@dataclass
class DownloadEvent:
    show_id: str
    episode_id: str
    ip_hash: str
    user_agent: str = ""
    method: str = "GET"
    byte_range: str = ""
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Row:
    """De-duplicated downloads for one episode, app string and day."""

    day: date
    episode_id: str
    user_agent: str
    downloads: int


@dataclass
class Summary:
    days: int | None
    total: int = 0
    last_7_days: int = 0
    today: int = 0
    by_day: list[tuple[date, int]] = field(default_factory=list)
    by_episode: dict[str, int] = field(default_factory=dict)
    by_app: list[tuple[str, int]] = field(default_factory=list)


# --- counting rules ------------------------------------------------------------

def hash_ip(ip: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


def is_countable(method: str, byte_range: str) -> bool:
    """Is this request the start of a real download?"""
    if method.upper() != "GET":
        return False
    r = (byte_range or "").replace(" ", "").lower()
    return r == "" or (r.startswith("bytes=0-") and r != "bytes=0-1")


def is_bot(user_agent: str) -> bool:
    return not (user_agent or "").strip() or bool(BOT_RE.search(user_agent))


def app_name(user_agent: str) -> str:
    for name, pattern in _APP_RES:
        if pattern.search(user_agent or ""):
            return name
    return "Other"


def since_for(days: int | None, today: date) -> date | None:
    """First day of a ``days``-long window ending today (None means all time)."""
    return None if days is None else today - timedelta(days=days - 1)


def summarize(rows: list[Row], days: int | None, today: date | None = None) -> Summary:
    """Turn de-duplicated rows into what the dashboard shows."""
    today = today or datetime.now(UTC).date()
    since = since_for(days, today)
    week_start = today - timedelta(days=6)
    summary = Summary(days=days)
    per_day: Counter[date] = Counter()
    per_episode: Counter[str] = Counter()
    per_app: Counter[str] = Counter()

    for row in rows:
        if is_bot(row.user_agent) or row.day > today or (since and row.day < since):
            continue
        summary.total += row.downloads
        per_day[row.day] += row.downloads
        per_episode[row.episode_id] += row.downloads
        per_app[app_name(row.user_agent)] += row.downloads
        if row.day >= week_start:
            summary.last_7_days += row.downloads
        if row.day == today:
            summary.today += row.downloads

    first = since or (min(per_day) if per_day else today)
    span = (today - first).days + 1
    summary.by_day = [(first + timedelta(days=i), per_day[first + timedelta(days=i)])
                      for i in range(span)]
    summary.by_episode = dict(per_episode)
    summary.by_app = sorted(per_app.items(), key=lambda kv: (-kv[1], kv[0]))
    return summary


def load_salt(storage: Storage, create: bool = True) -> str | None:
    """The deployment's IP-hashing salt.

    With ``create`` it is made on first use. The public service passes
    ``create=False``, because it has read-only access to storage, and gets
    ``None`` until the admin service has created the salt.
    """
    data, _ = storage.read_json(SALT_PATH)
    if not data and not create:
        return None
    if not data:
        try:
            storage.write_json(SALT_PATH, {"salt": secrets.token_hex(16)}, if_generation_match=0)
        except Conflict:
            pass  # another instance created it first
        data, _ = storage.read_json(SALT_PATH)
    return data["salt"]


# --- backends ------------------------------------------------------------------

class StatsStore:
    def record(self, event: DownloadEvent) -> None:
        raise NotImplementedError

    def rows(self, show_id: str, since: date | None) -> list[Row]:
        """Countable, de-duplicated downloads on or after ``since`` (bots included)."""
        raise NotImplementedError

    def describe(self) -> str:
        """Where the numbers come from, for the dashboard footer."""
        raise NotImplementedError


class LocalStats(StatsStore):
    """JSON-lines file, for development and tests."""

    def __init__(self, directory: str | Path):
        self.path = Path(directory) / "downloads.jsonl"
        self._lock = threading.Lock()

    def record(self, event):
        line = json.dumps(event.to_dict(), sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def rows(self, show_id, since):
        if not self.path.exists():
            return []
        listeners: dict[tuple[date, str, str], set[str]] = {}
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    day = datetime.fromisoformat(e["ts"]).astimezone(UTC).date()
                except (ValueError, KeyError):
                    continue  # skip a damaged line rather than lose the page
                if e.get("show_id") != show_id or (since and day < since):
                    continue
                if not is_countable(e.get("method", ""), e.get("byte_range", "")):
                    continue
                key = (day, e.get("episode_id", ""), e.get("user_agent", ""))
                listeners.setdefault(key, set()).add(e.get("ip_hash", ""))
        return [Row(day, ep, ua, len(ips)) for (day, ep, ua), ips in sorted(listeners.items())]

    def describe(self):
        return f"Local file {self.path} (development)."


class BigQueryStats(StatsStore):
    """Structured logs out, BigQuery queries in.

    ``table`` is the table the Cloud Logging sink writes to, as
    ``project.dataset.table``. Log entries land with the event's fields under
    ``jsonPayload`` and the time under ``timestamp``.
    """

    def __init__(self, table: str, stream=None):
        if not BQ_TABLE_RE.match(table):
            raise ValueError("BUCKETCAST_STATS_BQ_TABLE must look like project.dataset.table")
        self.table = table
        self._stream = stream or sys.stdout
        self._client = None

    def record(self, event):
        # App Engine turns a JSON line on stdout into a structured log entry.
        payload = {"event": EVENT_NAME, "severity": "INFO",
                   "message": f"download {event.show_id}/{event.episode_id}", **event.to_dict()}
        print(json.dumps(payload, sort_keys=True), file=self._stream, flush=True)

    def query(self, has_since: bool) -> str:
        since_clause = "AND timestamp >= @since" if has_since else ""
        return f"""
            SELECT
              DATE(timestamp) AS day,
              jsonPayload.episode_id AS episode_id,
              IFNULL(jsonPayload.user_agent, '') AS user_agent,
              COUNT(DISTINCT jsonPayload.ip_hash) AS downloads
            FROM `{self.table}`
            WHERE jsonPayload.event = '{EVENT_NAME}'
              AND jsonPayload.show_id = @show_id
              AND UPPER(jsonPayload.method) = 'GET'
              AND (
                IFNULL(jsonPayload.byte_range, '') = ''
                OR (
                  STARTS_WITH(LOWER(REPLACE(jsonPayload.byte_range, ' ', '')), 'bytes=0-')
                  AND LOWER(REPLACE(jsonPayload.byte_range, ' ', '')) != 'bytes=0-1'
                )
              )
              {since_clause}
            GROUP BY day, episode_id, user_agent
        """

    def rows(self, show_id, since):
        from google.cloud import bigquery  # imported lazily so dev/tests don't need it

        if self._client is None:
            self._client = bigquery.Client()
        params = [bigquery.ScalarQueryParameter("show_id", "STRING", show_id)]
        if since:
            start = datetime(since.year, since.month, since.day, tzinfo=UTC)
            params.append(bigquery.ScalarQueryParameter("since", "TIMESTAMP", start))
        job = self._client.query(
            self.query(bool(since)),
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        )
        return [Row(r["day"], r["episode_id"], r["user_agent"], r["downloads"])
                for r in job.result()]

    def describe(self):
        return f"BigQuery table {self.table}."


def stats_from_config(config: dict) -> StatsStore:
    if config.get("STATS_BQ_TABLE"):
        return BigQueryStats(config["STATS_BQ_TABLE"])
    return LocalStats(config.get("STATS_LOCAL_DIR", ".local-stats"))

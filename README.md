# Bucketcast

**A very lightweight, self-hosted podcast host for Google Cloud.**

Bucketcast publishes one or many podcasts from a single small Python app on
Google App Engine, with every episode, feed and page stored in a Google Cloud
Storage bucket. It's meant for independent podcasters with small audiences who
want to stop paying a hosting service per show. At low traffic it should run
almost entirely inside Google Cloud's free tier, so your main cost is your
domain names.

> **Status:** pre-alpha. The admin panel can create shows, upload episodes and
> publish an RSS feed and a show page; deployment is in progress (see the [issues](https://github.com/ryanthompson591/bucketcast/issues)).

## Why

Commercial hosts (Libsyn, Buzzsprout, Transistor, …) charge per show, so
starting a new podcast means paying again. Existing open-source hosts like
[Castopod](https://castopod.org) are excellent but full-featured. They need a
PHP app, a MySQL/MariaDB database and usually Redis, which is more than a
handful of shows with a few downloads a month needs.

Bucketcast aims to be the smallest thing that works:

- **No containers to manage.** Deploy straight from Python source to App Engine
  standard.
- **No database server.** Each show's metadata is a JSON file in the bucket.
- **Unlimited shows.** Starting a new podcast means buying a domain, mapping it,
  and clicking "New show."
- **Scale to zero.** When nobody's uploading or browsing, nothing is running.

## Architecture

```
            ┌───────────────────────────────┐
 Admin ───► │ App Engine (Flask)            │
 (you)      │  • admin UI (behind IAP)      │──┐ signed upload URL
            │  • rebuilds feed.xml + pages  │  │
            │  • serves static site/feed    │  │
            └──────────────┬────────────────┘  │
                           │ read/write        ▼
            ┌──────────────▼────────────────────────────┐
            │ Cloud Storage bucket                      │
            │  shows/<show-id>/                         │
            │    show.json, episodes.json               │
            │    feed.xml, index.html                   │
            │    artwork/…  audio/…                     │
            └──────────────┬────────────────────────────┘
                           │ direct download (range requests)
 Listeners / podcast apps ─┘
```

Key design decisions:

1. **Audio never passes through the app.** App Engine caps requests at about
   32 MB, and episodes are often larger. Uploads go straight from the browser
   to Cloud Storage using a short-lived **signed URL**. Downloads go straight
   from the bucket, which supports the byte-range requests podcast apps use for
   seeking and resuming.
2. **Feeds and pages are static.** Publishing an episode regenerates that
   show's `feed.xml` and `index.html`. Apple, Spotify and other podcast
   directories only ever fetch static files.
3. **Multi-tenant by domain.** Several custom domains map to one App Engine
   app. The app picks the show from the request's `Host` header.
4. **Auth by Identity-Aware Proxy.** The `/admin` routes sit behind Google
   IAP, so only your Google account can reach them, with no login code to
   write.
5. **Free-tier friendly.** Put the bucket in `us-east1`, `us-west1` or
   `us-central1` to use Cloud Storage's Always Free allowance (5 GB of storage
   and 100 GB of North America egress per month at time of writing).

## MVP scope

- [ ] **Admin UI:** create and edit shows (title, description, artwork,
      author, category, language, explicit flag, domain)
- [ ] **Episode upload:** signed-URL direct upload to Cloud Storage, plus
      title, description/show notes, publish date, episode/season number
- [ ] **RSS feed generation:** valid podcast RSS that meets Apple Podcasts
      requirements, with iTunes and Podcasting 2.0 namespaces where useful
- [ ] **Static show site:** a simple homepage per show listing episodes with an
      HTML5 audio player
- [ ] **Multi-show routing:** host-header-based routing across custom domains
- [ ] **Libsyn import:** a CLI that reads an existing RSS feed, copies audio
      and artwork into the bucket, and **preserves episode GUIDs** so
      subscribers don't re-download the back catalog
- [ ] **Feed migration support:** `<itunes:new-feed-url>` so apps follow you
      after you redirect your old feed
- [ ] **Deploy docs:** step-by-step App Engine, bucket, IAP and custom domain
      setup

## Later (maybe)

- Download stats through a tiny logging redirect in front of audio files
- Scheduled publishing
- Transcripts and chapters (Podcasting 2.0)
- Additional storage backends (S3-compatible)

## Moving a show from another host

Listeners follow a show by its feed address, so moving hosts means sending
that address to the new feed without losing anyone. Two things do it
together, and both are needed:

- **A redirect at the old host** (a permanent, "301" redirect from the old
  feed address to the new one). This is what moves existing subscribers:
  their apps ask the old address, get sent to the new one, and remember it.
- **`<itunes:new-feed-url>` in the new feed**, which tells Apple Podcasts and
  other directories that the new address is the show's real home. Bucketcast
  adds it while the show's **Previous feed address** setting is filled in.

Episode GUIDs must stay the same across the move, or apps treat every
episode as new and download the back catalogue again.

Steps:

1. Bring the show's episodes across with their original GUIDs, using the
   importer (see *Importing an existing show* below).
2. Point the show's domain at Bucketcast (see *Custom domains*) and check
   the new feed at `https://yourdomain.com/feed.xml` in a feed validator.
3. In the show's settings, put the old feed address in **Previous feed
   address** and save.
4. At the old host, set up the redirect to `https://yourdomain.com/feed.xml`.
   On Libsyn this is a "Redirect Feed URL" setting under the feed's advanced
   options; their help pages have the current steps.
5. Subscribe to the old address in a podcast app and confirm new episodes
   arrive from the new feed.
6. Keep the old account active for at least four weeks, so apps that check
   rarely still see the redirect. Then cancel it, and clear **Previous feed
   address** after a few months.

If the domain itself is staying the same and only the hosting behind it is
changing, subscribers keep the same feed address only if the path matches
too. Otherwise the steps above still apply.

## Importing an existing show

```bash
python -m bucketcast import --show my-show --feed https://example.com/old/rss --dry-run
python -m bucketcast import --show my-show --feed https://example.com/old/rss
```

The importer reads any standard podcast feed (Libsyn's included) and copies
the show into the bucket, or into `.local-bucket` when no bucket is set:

- **Show**: title, description, author, owner, category, language, explicit
  flag and artwork. An existing show keeps its own settings.
- **Episodes**: title, publish date, season and episode numbers, explicit
  flag, duration, thumbnail, and the audio file itself.
- **GUIDs are kept exactly**, along with the show's `podcast:guid` if it has
  one, so subscribers' apps don't download the back catalogue again.
- **Notes** are converted from HTML to Markdown, and images in them are
  copied into the show's assets.
- The old feed's address is saved as the show's *Previous feed address*.

Use `--dry-run` first: it lists what would be imported and changes nothing.
`--limit N` imports at most N episodes per run, oldest first. Re-running is
safe; episodes already imported (or imported and then deleted) are skipped,
and one failed episode doesn't stop the others.

Limits: audio must be MP3 or M4A, and feed items with no audio file (blog or
video posts) are skipped, since every Bucketcast episode has audio.

## Tech stack

- Python 3 + Flask on App Engine standard
- Google Cloud Storage (`google-cloud-storage`)
- Jinja2 templates for the feed and pages
- No JavaScript framework. Plain HTML forms, plus a little JS for direct
  uploads.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# No Google Cloud needed locally: files go to ./.local-bucket, and admin auth is bypassed.
BUCKETCAST_DEV_AUTH_BYPASS=1 flask --app main run --debug
# open http://127.0.0.1:5000/admin/

pytest && ruff check .
```

Configuration (environment variables, set in `app.yaml` for App Engine):

| Variable | Purpose |
|---|---|
| `BUCKETCAST_BUCKET` | Cloud Storage bucket name. If unset, uses local storage in `BUCKETCAST_LOCAL_DIR` (default `.local-bucket`). |
| `BUCKETCAST_ROLE` | Which half of the app to serve: `public`, `admin`, or `all` (default; both, for local development). |
| `BUCKETCAST_IAP_AUDIENCE` | IAP audience (`/projects/<number>/apps/<project-id>`). Admin is locked until this is set. |
| `BUCKETCAST_ADMIN_EMAILS` | Comma-separated Google accounts allowed to administer. **Required**: admin is locked until at least one is listed. |
| `BUCKETCAST_DEV_AUTH_BYPASS` | `1` to skip admin auth. **Local development only**; ignored on Google Cloud. |
| `BUCKETCAST_STATS_BQ_TABLE` | BigQuery table the download-log sink writes to, as `project.dataset.table`. If unset, stats use a local file in `BUCKETCAST_STATS_LOCAL_DIR` (default `.local-stats`). |
| `BUCKETCAST_PUBLIC_BASE_URL` | Base for media URLs in the feed when using local storage. Also the base of the download-counting links for shows without a custom domain, so on Google Cloud set it to the public service's URL. Defaults to the address you opened the admin at. |

## Episode uploads

Audio goes straight from the browser to the bucket. The admin page asks the
app for a signed URL (valid for one hour), `PUT`s the file to it with a
progress bar, and then saves the episode. The app records the file's size from
the bucket and the duration measured by the browser in
`shows/<show-id>/episodes.json`.

Locally there is no bucket, so a dev-only route (`/_local_upload/…`) stands in
for the signed URL and writes into `.local-bucket`.

On Google Cloud, two one-time settings are needed:

1. **Bucket CORS**, so the browser may `PUT` to the bucket from the admin
   page. Put your admin origin in [`cors.json`](cors.json), then:

   ```bash
   gcloud storage buckets update gs://YOUR-BUCKET --cors-file=cors.json
   ```

2. **Permission to sign URLs.** App Engine's service account has no private
   key, so the app signs through the IAM API. Enable the *IAM Service Account
   Credentials API* and give the service account the *Service Account Token
   Creator* role on itself:

   ```bash
   gcloud services enable iamcredentials.googleapis.com
   gcloud iam service-accounts add-iam-policy-binding \
     YOUR-PROJECT@appspot.gserviceaccount.com \
     --member="serviceAccount:YOUR-PROJECT@appspot.gserviceaccount.com" \
     --role="roles/iam.serviceAccountTokenCreator"
   ```

## RSS feed

Each show's feed is a static file, `shows/<show-id>/feed.xml`, rebuilt every
time the show or one of its episodes is saved or deleted. The Episodes page
shows its URL, a **Rebuild feed** button, and anything Apple Podcasts still
requires (artwork, description, author, owner email).

- RSS 2.0 with `itunes:` tags, `content:encoded` show notes, and
  `podcast:guid` / `podcast:locked`.
- Only episodes whose publish date has passed are included. A future-dated
  episode appears the next time the feed is rebuilt after that date; nothing
  rebuilds it automatically yet.
- Deleting a show removes its feed.

## Show page

Each show also gets a static web page, rebuilt alongside the feed:
`shows/<show-id>/index.html`, plus `page/<n>/index.html` for older episodes.
It lists episodes newest first, each with an audio player, its thumbnail and
its notes, and works with no JavaScript.

Two settings in the show's form control the layout:

- **Episodes per page** (default 30). Older episodes go on further pages,
  linked with Next and Previous.
- **Episode notes**: shown in full, images included (the default), or as a
  short plain-text excerpt.

Links between the generated files are relative, so the same files work at the
show's own domain and straight from the bucket.

## Admin access

In production the app runs as **two App Engine services from the same code**:

| Service | Config | Serves | Login |
|---|---|---|---|
| `default` (public) | `app.yaml` | show pages, feeds, download redirect | none |
| `admin` | `admin.yaml` | `/admin` | Google sign-in through IAP |

Identity-Aware Proxy protects whole services, not URL paths, so the admin is
its own service instead of a path on the public one. Each service only
registers its own routes: the public service has no admin code paths to
reach, and the admin service has no public ones.

Three checks guard the admin, and all must pass:

1. **IAP** requires a Google sign-in before a request reaches the app.
2. **The app verifies IAP's signed token** on every admin request, so a
   request that gets around IAP is refused.
3. **The signed-in account must be in `BUCKETCAST_ADMIN_EMAILS`.** The list
   is required. With it empty the admin refuses everyone, so a mistake in
   IAP's own access settings can't let in every Google account.

A fresh deploy with nothing configured is locked. `BUCKETCAST_DEV_AUTH_BYPASS`
skips all of this for local development and is ignored on Google Cloud.

## Custom domains

One app serves every show. A request's `Host` header picks the show whose
**Domain** setting matches (with or without `www.`), and the app serves that
show's static files from the bucket:

| Address | Serves |
|---|---|
| `https://yourdomain.com/` | the show page |
| `https://yourdomain.com/page/2/` | older episodes |
| `https://yourdomain.com/feed.xml` | the RSS feed |
| `https://yourdomain.com/e/...` | the download-counting redirect |

A show with a domain advertises `https://yourdomain.com/feed.xml` as its feed
address; that is the one to submit to podcast directories. An address no
show claims gets a plain "No podcast here" page. The domain-to-show map is
cached for a minute, so a changed domain takes up to that long to apply.

On Google Cloud each domain is mapped to the App Engine app once, which also
issues its certificate:

```bash
gcloud app domain-mappings create yourdomain.com
gcloud app domain-mappings create www.yourdomain.com
```

Then add the DNS records the command prints at your registrar.

To try it locally, give a show a domain ending in `.localhost` (for example
`myshow.localhost`) and open `http://myshow.localhost:5000/`; browsers send
those names to your own machine.

## Download stats

Optional, per show. With **Count downloads** on in a show's settings, the
feed's audio links point at `/e/<show-id>/<episode-id>.mp3` on the app. That
endpoint records the request and redirects to the file in the bucket; the
audio still never passes through the app. The show's **Stats** page charts
downloads per day and breaks them down by episode and by app.

A download is counted once per listener, app and episode per UTC day. `HEAD`
requests, seeks and resumes (range requests that don't start at byte 0), the
2-byte `bytes=0-1` probe and known bots are left out. Listeners are told
apart by a salted hash of their IP address; the address itself is not stored.
Events are kept raw and counted when read, so the rules can change later
without losing history.

Locally, events go to `.local-stats/downloads.jsonl`. On Google Cloud they go
to **BigQuery** by way of logging, so BigQuery can never slow down or break a
download:

1. The endpoint prints one structured log line per request.
2. A Cloud Logging sink copies those lines into a BigQuery dataset.
3. The Stats page queries that table.

One-time setup (not yet run against a real project, so check the names as you
go, in particular the table the sink creates):

```bash
bq mk --dataset YOUR-PROJECT:bucketcast_logs

gcloud logging sinks create bucketcast-downloads \
  bigquery.googleapis.com/projects/YOUR-PROJECT/datasets/bucketcast_logs \
  --log-filter='jsonPayload.event="bucketcast.download"' \
  --use-partitioned-tables
```

Then give the sink's writer identity (printed by the command above) the
*BigQuery Data Editor* role on the dataset, give the app's service account
*BigQuery Job User* and *BigQuery Data Viewer*, and set
`BUCKETCAST_STATS_BQ_TABLE` to the table the sink writes to
(`YOUR-PROJECT.bucketcast_logs.<table>`).

Turning counting on puts the app in the download path: if the app is down,
downloads fail until it is back, whereas direct bucket links keep working.

## Show notes and assets

Episode notes are written in Markdown and rendered to HTML wherever they are
shown. Raw HTML in notes is not passed through; it appears as text.

Each show has an asset library (`shows/<show-id>/assets/`) for images of any
shape. In the episode form, **Insert image** picks or uploads one and adds it
to the notes as `![description](asset:file-name.jpg)`. Notes refer to assets
by name, not by URL, so they keep working if the bucket or domain changes.

## License

[MIT](LICENSE)

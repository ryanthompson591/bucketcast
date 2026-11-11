# Bucketcast

**A very lightweight, self-hosted podcast host for Google Cloud.**

Bucketcast publishes one or many podcasts from a single small Python app on
Google App Engine, with every episode, feed and page stored in a Google Cloud
Storage bucket. It's meant for independent podcasters with small audiences who
want to stop paying a hosting service per show. At low traffic it should run
almost entirely inside Google Cloud's free tier, so your main cost is your
domain names.

> **Status:** pre-alpha. The show admin panel works; episodes, feeds and public
> pages are in progress (see the [issues](https://github.com/ryanthompson591/bucketcast/issues)).

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

## Migrating from Libsyn (planned flow)

1. `bucketcast import --show my-show --feed https://…libsyn…/rss`
2. Check the new feed at `https://yourdomain/feed.xml`.
3. In Libsyn, redirect your old feed to the new URL.
4. Leave Libsyn active for a few weeks while podcast apps and directories
   follow the redirect, then cancel.

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
| `BUCKETCAST_IAP_AUDIENCE` | IAP audience (`/projects/<number>/apps/<project-id>`). Admin is locked until this is set. |
| `BUCKETCAST_ADMIN_EMAILS` | Optional comma-separated allow-list of admin Google accounts. |
| `BUCKETCAST_DEV_AUTH_BYPASS` | `1` to skip admin auth. **Local development only.** |

## License

[MIT](LICENSE)

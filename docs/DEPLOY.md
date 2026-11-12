# Deploying Bucketcast to Google Cloud

This guide takes you from an empty project to a running Bucketcast: two
buckets, two narrowly-permissioned service accounts, the public and admin
services on App Engine, Google sign-in on the admin, your own domain, and
(optionally) download stats in BigQuery.

> **Status of this guide.** The commands were checked against Google's
> documentation in November 2026, but the guide as a whole has not yet been
> run end to end against a real project. Steps that could not be confirmed
> from the documentation are marked **Unconfirmed**. Expect to adjust a few
> details on the first deploy, and please fix the guide when you do.

Commands are written on one line each so they work in PowerShell, Command
Prompt and bash alike. Replace the capitalised placeholders:

| Placeholder | Meaning | Example |
|---|---|---|
| `PROJECT_ID` | your Google Cloud project ID | `my-podcasts` |
| `PUBLIC` | bucket for what listeners download | `my-podcasts-public` |
| `PRIVATE` | bucket for show and episode data | `my-podcasts-private` |
| `YOU@gmail.com` | the Google account that will administer shows | |

Everything goes in one region. This guide uses `us-central1`, which App
Engine calls `us-central`.

## The security design

Only what listeners download is public. Everything else is reachable only by
the app, and each half of the app can do only what it needs.

**Two buckets**

| Bucket | Holds | Who can read it |
|---|---|---|
| `PUBLIC` | audio, artwork, images in notes, `feed.xml`, show pages | anyone, by exact address (no browsing) |
| `PRIVATE` | `show.json`, `episodes.json`, the stats secret | the two service accounts below, and project owners |

The app decides which bucket a file belongs to in one place
(`is_public_path` in `bucketcast/storage.py`), and anything not explicitly
listed as public goes to the private bucket.

**Two service accounts, one per service**

| Account | Runs | Can do |
|---|---|---|
| `bucketcast-public` | the public service | read objects in both buckets; write logs |
| `bucketcast-admin` | the admin service | read, write and delete objects in both buckets; sign upload links as itself; query the stats dataset; write logs |

Neither can change a bucket's settings or permissions, create resources, or
touch anything else in the project. The public service has no write access
at all, so a flaw in it can't be used to alter a feed or upload a file.

**People**

| Who | Access |
|---|---|
| You | project Owner; you deploy |
| Admins | the admin service only: Google sign-in through IAP, the app's check of IAP's signed token, and the `BUCKETCAST_ADMIN_EMAILS` list must all pass |
| The public | files in `PUBLIC`, and the public service |

## What you need

- A Google Cloud project **with billing enabled**. Google requires a billing
  account even for free-tier use.
- The [`gcloud` command-line tool](https://cloud.google.com/sdk/docs/install),
  signed in as a project Owner.
- This repository checked out.

```bash
gcloud auth login
```

```bash
gcloud config set project PROJECT_ID
```

Turn on the services Bucketcast uses:

```bash
gcloud services enable appengine.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com iam.googleapis.com iamcredentials.googleapis.com iap.googleapis.com storage.googleapis.com bigquery.googleapis.com logging.googleapis.com
```

## 1. Create the two service accounts

```bash
gcloud iam service-accounts create bucketcast-public --display-name="Bucketcast public service"
```

```bash
gcloud iam service-accounts create bucketcast-admin --display-name="Bucketcast admin service"
```

Let both write logs:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:bucketcast-public@PROJECT_ID.iam.gserviceaccount.com" --role="roles/logging.logWriter" --condition=None
```

```bash
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com" --role="roles/logging.logWriter" --condition=None
```

Let the admin account sign upload links as itself. App Engine service
accounts have no private key, so the app signs through Google's IAM API:

```bash
gcloud iam service-accounts add-iam-policy-binding bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com --member="serviceAccount:bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com" --role="roles/iam.serviceAccountTokenCreator"
```

## 2. Create the two buckets

The free tier (5 GB of storage and 100 GB of North America egress a month,
at the time of writing) applies in `us-central1`, `us-east1` and `us-west1`.
A bucket's location can't be changed later.

### The private bucket

```bash
gcloud storage buckets create gs://PRIVATE --location=us-central1 --uniform-bucket-level-access
```

Enforce public access prevention, so it can never be made public by a later
mistake:

```bash
gcloud storage buckets update gs://PRIVATE --public-access-prevention
```

Grant the two accounts their access to it:

```bash
gcloud storage buckets add-iam-policy-binding gs://PRIVATE --member="serviceAccount:bucketcast-public@PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.objectViewer"
```

```bash
gcloud storage buckets add-iam-policy-binding gs://PRIVATE --member="serviceAccount:bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.objectUser"
```

### The public bucket

```bash
gcloud storage buckets create gs://PUBLIC --location=us-central1 --uniform-bucket-level-access
```

```bash
gcloud storage buckets add-iam-policy-binding gs://PUBLIC --member="serviceAccount:bucketcast-public@PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.objectViewer"
```

```bash
gcloud storage buckets add-iam-policy-binding gs://PUBLIC --member="serviceAccount:bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.objectUser"
```

Let anyone fetch a file by its address, without being able to list the
bucket's contents. `legacyObjectReader` allows reading an object but not
listing; the more common `objectViewer` would also let anyone browse the
bucket, which would reveal uploaded-but-unpublished episodes.

```bash
gcloud storage buckets add-iam-policy-binding gs://PUBLIC --member=allUsers --role=roles/storage.legacyObjectReader
```

**Unconfirmed:** that this legacy role can be granted on a bucket with
uniform bucket-level access. If the command is refused, the fallback is
`--role=roles/storage.objectViewer`, accepting that the bucket can then be
listed; step 5 has a check that tells you which you ended up with.

### Allow browser uploads to the public bucket (CORS)

The admin page uploads audio from your browser straight to the public
bucket. Copy `cors.example.json` to `cors.json` and replace the placeholder
with the address of your **admin** service, which you'll know after step 3
(it looks like `https://admin-dot-PROJECT_ID.REGION.r.appspot.com`; use the
exact address `gcloud app deploy` prints, with no trailing slash). Then:

```bash
gcloud storage buckets update gs://PUBLIC --cors-file=cors.json
```

## 3. Deploy the two services

Create the App Engine app once. **The region is permanent.**

```bash
gcloud app create --region=us-central
```

Copy the two templates. The copies are ignored by git, so your settings
stay out of the repository:

- `app.example.yaml` → `app.yaml`
- `admin.example.yaml` → `admin.yaml`

In both, replace `PROJECT_ID` in the `service_account` line and set the two
bucket names. In `admin.yaml` also set:

- `BUCKETCAST_ADMIN_EMAILS`: your Google account. Required.
- `BUCKETCAST_PUBLIC_BASE_URL`: the public service's address,
  `https://PROJECT_ID.REGION.r.appspot.com`.
- `BUCKETCAST_IAP_AUDIENCE`: `/projects/PROJECT_NUMBER/apps/PROJECT_ID`. Get
  the number with:

```bash
gcloud projects describe PROJECT_ID --format="value(projectNumber)"
```

Deploy both:

```bash
gcloud app deploy app.yaml admin.yaml
```

At this point the admin answers "Admin is locked" or "Missing IAP
assertion" to everyone. That is correct: it stays shut until step 5.

### The default App Engine service account

Creating the app also creates `PROJECT_ID@appspot.gserviceaccount.com`.
Neither Bucketcast service runs as it, but depending on the project it may
have been given the broad Editor role automatically. Check:

```bash
gcloud projects get-iam-policy PROJECT_ID --flatten="bindings[].members" --filter="bindings.members:PROJECT_ID@appspot.gserviceaccount.com" --format="table(bindings.role)"
```

If it lists `roles/editor`, that is more access than anything here needs.
**Unconfirmed:** whether deploys rely on it. The cautious order is: finish
this guide, then remove the role, deploy again, and put it back if the
deploy fails.

```bash
gcloud projects remove-iam-policy-binding PROJECT_ID --member="serviceAccount:PROJECT_ID@appspot.gserviceaccount.com" --role="roles/editor"
```

## 4. Set up Google sign-in (OAuth) for a personal project

Skip this step if your project belongs to a Google Workspace organisation
and only people in it will sign in.

A project owned by a personal Gmail account needs its own OAuth client
before Identity-Aware Proxy (IAP) can be turned on:

1. In the console, open **Google Auth Platform → Branding**
   (`https://console.cloud.google.com/auth/branding`) and click **Get
   started**. Enter an app name and your email, choose **External** as the
   audience, and create it.
2. Open **APIs & Services → Credentials → Create credentials → OAuth client
   ID**. Choose **Web application**, name it, and create it. Note the client
   ID and secret. Treat the secret like a password: don't commit it or paste
   it anywhere but the command in step 5.
3. Open the client again and add this under **Authorised redirect URIs**,
   with your client ID in place of `CLIENT_ID`:

   ```
   https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect
   ```

**Unconfirmed:** whether an External app left in "Testing" needs your account
added as a test user before you can sign in. If sign-in is refused, add
yourself under **Audience → Test users**.

## 5. Turn on Identity-Aware Proxy

IAP is switched on for the whole App Engine app, then access is granted per
service.

Enable it (leave out the two `--oauth2` flags if you skipped step 4):

```bash
gcloud iap web enable --resource-type=app-engine --oauth2-client-id=CLIENT_ID --oauth2-client-secret=CLIENT_SECRET
```

**The moment IAP is on, the public service asks for a login too.** Open it
back up to everyone, and grant yourself the admin:

```bash
gcloud iap web add-iam-policy-binding --resource-type=app-engine --service=default --member=allUsers --role=roles/iap.httpsResourceAccessor
```

```bash
gcloud iap web add-iam-policy-binding --resource-type=app-engine --service=admin --member=user:YOU@gmail.com --role=roles/iap.httpsResourceAccessor
```

Google's documentation confirms that App Engine services can have separate
IAP policies and that `allUsers` can be granted this role. **Unconfirmed:**
this exact pair of commands; if the first is refused, set the same access
in the console under **Security → Identity-Aware Proxy**, on the `default`
service's row.

If this step is misconfigured, the failure is a locked admin or a
login-walled public site, never an open admin: the app's own token check and
allow-list still stand between a visitor and the admin.

## 6. Check it works, and check it's locked down

Work through these in order. Each exercises something that has only been
tested locally so far.

**Function**

- [ ] `https://PROJECT_ID.REGION.r.appspot.com/healthz` answers `{"ok": true}`
      with no login.
- [ ] The admin address asks for a Google sign-in, lets your account in, and
      shows your email in the top corner.
- [ ] Create a show with artwork. The artwork displays.
- [ ] Upload a large episode, ideally 150 MB or more. The progress bar
      completes and the episode saves. If the upload fails immediately,
      re-check the origin in `cors.json`.
- [ ] Upload an asset, insert it into an episode's notes, delete another
      asset.
- [ ] Open the show page and feed links from the Episodes page. Audio plays.
- [ ] Run the feed address through a validator such as
      [Podbase](https://podba.se/validate/) or Cast Feed Validator.

**Security.** Open these in a private browser window, signed out:

- [ ] `https://storage.googleapis.com/PRIVATE/shows/SHOW_ID/show.json` is
      refused (Access Denied). So is `.../episodes.json` and
      `https://storage.googleapis.com/PRIVATE/config/stats-salt.json`.
- [ ] `https://storage.googleapis.com/PUBLIC/shows/SHOW_ID/show.json` is Not
      Found: the data files were never written to the public bucket.
- [ ] `https://storage.googleapis.com/PUBLIC/` is refused. If it shows a
      list of files instead, the bucket is listable (you're on the
      `objectViewer` fallback from step 2).
- [ ] `https://storage.googleapis.com/PUBLIC/shows/SHOW_ID/feed.xml` loads.
- [ ] The admin address, signed in as a **different** Google account, is
      refused.
- [ ] `https://PROJECT_ID.REGION.r.appspot.com/admin/` (the public service)
      is Not Found.

And from your terminal, confirm the public service's account cannot write:

```bash
gcloud storage buckets get-iam-policy gs://PRIVATE --format="table(bindings.role,bindings.members.flatten())"
```

`bucketcast-public` should appear only against `roles/storage.objectViewer`,
and `allUsers` should not appear at all.

## 7. Use your own domain

Set the show's **Domain** in its settings, then map the domain to the app.
Mapping issues a free, automatically renewed certificate.

```bash
gcloud app domain-mappings create yourdomain.com
```

```bash
gcloud app domain-mappings create www.yourdomain.com
```

You must first prove you own the domain. In the console this is part of
**App Engine → Settings → Custom domains → Add a custom domain**, which
walks you through verification and then shows the DNS records to add at your
registrar: `A`/`AAAA` records for the bare domain and a `CNAME` for `www`.
The certificate can take several minutes to issue after DNS is in place.

**Unconfirmed:** that a mapped domain goes to the `default` (public) service
without further configuration. It should, since that is what "default"
means, but check that `https://yourdomain.com/admin/` returns Not Found.

Afterwards:

- [ ] `https://yourdomain.com/` shows the show page and
      `https://yourdomain.com/feed.xml` the feed.
- [ ] A second show on a second domain shows its own page, not the first's.
- [ ] Rebuild the show (Episodes page → Rebuild) so its feed advertises the
      new address.

## 8. Download stats in BigQuery (optional)

Create the dataset in the same region. The expiry (in seconds; this is 400
days) makes BigQuery drop old download records automatically:

```bash
bq --location=us-central1 mk --dataset --default_partition_expiration=34560000 PROJECT_ID:bucketcast_logs
```

Create a sink that copies Bucketcast's download log lines into it:

```bash
gcloud logging sinks create bucketcast-downloads bigquery.googleapis.com/projects/PROJECT_ID/datasets/bucketcast_logs --log-filter="jsonPayload.event=\"bucketcast.download\"" --use-partitioned-tables
```

Three grants, each as narrow as it can be:

1. **The sink may write to this one dataset.** Find its identity:

   ```bash
   gcloud logging sinks describe bucketcast-downloads --format="value(writerIdentity)"
   ```

   In the console, open BigQuery → `bucketcast_logs` → **Sharing →
   Permissions → Add principal**, and give that identity **BigQuery Data
   Editor**.
2. **The admin account may read this one dataset.** In the same dialog, give
   `bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com` **BigQuery Data
   Viewer**.
3. **The admin account may run queries.** This one is project-wide, because
   that is where query jobs are billed:

   ```bash
   gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:bucketcast-admin@PROJECT_ID.iam.gserviceaccount.com" --role="roles/bigquery.jobUser" --condition=None
   ```

The public service gets no BigQuery access. It only writes log lines.

Turn on **Count downloads** for a show, rebuild it, and play an episode
through the show page. Within about a minute a table appears:

```bash
bq ls bucketcast_logs
```

**Unconfirmed:** the table's name. By Google's naming rule it should be
`appengine_googleapis_com_stdout`, but use whatever `bq ls` shows. Put it in
`admin.yaml` as `BUCKETCAST_STATS_BQ_TABLE: "PROJECT_ID.bucketcast_logs.TABLE"`
and deploy the admin again.

- [ ] The show's Stats page shows the download you just made. If it shows an
      error instead, the admin's logs have the reason; the likeliest causes
      are the table name or a missing grant above.

What is stored per download: the show, the episode, the time, the app's
user-agent string, and a salted hash of the listener's IP address. The
address itself is never logged, and the salt lives in the private bucket.

Counting puts the app in the download path. If you'd rather downloads keep
working even when the app is down, leave it off; audio then comes straight
from the bucket and there are no stats.

## 9. Bring over an existing show

The importer runs on your own machine with your own Google credentials and
writes to the buckets. See the README's *Importing an existing show* and
*Moving a show from another host* for the full procedure.

```bash
gcloud auth application-default login
```

PowerShell:

```powershell
$env:BUCKETCAST_PUBLIC_BUCKET = "PUBLIC"; $env:BUCKETCAST_PRIVATE_BUCKET = "PRIVATE"
```

```bash
python -m bucketcast import --show my-show --feed https://example.com/old/rss --dry-run
```

If the real import is refused with a permission error, grant your own
account **Storage Object User** on both buckets; project Owner doesn't
always include reading and writing objects.

- [ ] After a real import, the new feed lists the same GUIDs as the old one.

## Updating

After pulling new code, deploy both services again:

```bash
gcloud app deploy app.yaml admin.yaml
```

`app.yaml`, `admin.yaml` and `cors.json` are yours and are ignored by git.
When the `*.example.*` templates change, compare and carry the changes over.

## Costs

Both services scale to zero, and the buckets, App Engine, logging and
BigQuery all have free tiers that a small show should stay inside. The
usual first cost is bucket egress once downloads pass about 100 GB a month.
Set a [budget alert](https://cloud.google.com/billing/docs/how-to/budgets)
so a surprise can't go unnoticed.

## If something goes wrong

| Symptom | Likely cause |
|---|---|
| Deploy fails mentioning the service account | the account in `service_account:` doesn't exist yet (step 1), or its name is mistyped |
| Admin says "Admin is locked" | `BUCKETCAST_IAP_AUDIENCE` or `BUCKETCAST_ADMIN_EMAILS` is empty in `admin.yaml` |
| Admin says "Invalid IAP assertion" | the audience has the wrong project number or ID |
| Admin says "not allowed" | you're signed in with an account not in `BUCKETCAST_ADMIN_EMAILS` |
| Admin shows a server error on every page | `bucketcast-admin` lacks `objectUser` on a bucket (step 2) |
| Public pages ask for a Google login | step 5's `allUsers` grant on the `default` service is missing |
| Public pages show "No podcast here" for a real domain | `bucketcast-public` lacks `objectViewer` on the private bucket, or the show's Domain setting doesn't match |
| Upload fails at 0% | `cors.json` origin doesn't exactly match the admin address, or CORS was set on the wrong bucket |
| "Could not start the upload" | the signing grant in step 1 is missing |
| Artwork and audio show as broken | the `allUsers` grant on the public bucket is missing (step 2) |
| Show page links point at `localhost` or the admin address | `BUCKETCAST_PUBLIC_BASE_URL` isn't set in `admin.yaml`; set it, deploy, and Rebuild |
| App fails to start: "BUCKETCAST_BUCKET is no longer used" | an old config; set the two bucket variables instead |

Logs for both services:

```bash
gcloud app logs tail
```

# Deploying Bucketcast to Google Cloud

This guide takes you from nothing to a running Bucketcast: a bucket, the
public and admin services on App Engine, Google sign-in on the admin, your
own domain, and (optionally) download stats in BigQuery.

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
| `BUCKET` | a globally unique bucket name | `my-podcasts-media` |
| `YOU@gmail.com` | the Google account that will administer shows | |

## What you need

- A Google account and a Google Cloud project **with billing enabled**.
  Google requires a billing account even for free-tier use.
- The [`gcloud` command-line tool](https://cloud.google.com/sdk/docs/install),
  signed in.
- This repository checked out.

```bash
gcloud auth login
```

```bash
gcloud config set project PROJECT_ID
```

## 1. Create the bucket

Pick `us-central1`, `us-east1` or `us-west1`: Cloud Storage's free tier
(5 GB of storage and 100 GB of North America egress a month, at the time of
writing) only applies in those regions. A bucket's location can't be changed
later.

```bash
gcloud storage buckets create gs://BUCKET --location=us-central1 --uniform-bucket-level-access
```

### Make the `shows/` folder public, and only that folder

Listeners fetch audio, artwork, feeds and pages straight from the bucket, so
those files must be publicly readable. Everything Bucketcast publishes lives
under `shows/`. Its private settings (such as the secret used to anonymise
listener addresses for stats) live under `config/` and must stay private, so
do **not** make the whole bucket public.

```bash
gcloud storage managed-folders create gs://BUCKET/shows/
```

```bash
gcloud storage managed-folders add-iam-policy-binding gs://BUCKET/shows --member=allUsers --role=roles/storage.objectViewer
```

This needs uniform bucket-level access (set above) and fails if "public
access prevention" is enforced on the bucket or by an organisation policy.

Be aware that `shows/` also holds each show's `show.json` and
`episodes.json`. Those contain what the feed already publishes, plus a few
things it doesn't: unpublished and deleted episodes' titles and audio paths.
Don't put anything in a show or episode that you wouldn't want found.

### Allow browser uploads (CORS)

The admin page uploads audio from your browser straight to the bucket. Edit
[`cors.json`](../cors.json) and replace the placeholder with the address of
your **admin** service, which you'll know after step 2 (it looks like
`https://admin-dot-PROJECT_ID.REGION.r.appspot.com`; use the exact address
`gcloud app deploy` prints, with no trailing slash). Then:

```bash
gcloud storage buckets update gs://BUCKET --cors-file=cors.json
```

## 2. Deploy the two services

Bucketcast runs as two App Engine services from the same code:

| Service | File | What it serves | Login |
|---|---|---|---|
| `default` (public) | `app.yaml` | show pages, feeds, download redirect | none |
| `admin` | `admin.yaml` | `/admin` | Google sign-in |

Create the App Engine app once. The region is permanent; pick one near the
bucket (`gcloud app regions list` shows the valid names, which differ
slightly from bucket regions, e.g. `us-central`).

```bash
gcloud app create --region=us-central
```

Fill in the settings in both files:

- **`app.yaml`**: `BUCKETCAST_BUCKET`.
- **`admin.yaml`**: `BUCKETCAST_BUCKET`, `BUCKETCAST_ADMIN_EMAILS` (your
  Google account; required), `BUCKETCAST_PUBLIC_BASE_URL` (the public
  service's address, `https://PROJECT_ID.REGION.r.appspot.com`), and
  `BUCKETCAST_IAP_AUDIENCE`, which is `/projects/PROJECT_NUMBER/apps/PROJECT_ID`.
  Get the project number with:

```bash
gcloud projects describe PROJECT_ID --format="value(projectNumber)"
```

Deploy both:

```bash
gcloud app deploy app.yaml admin.yaml
```

At this point the admin answers "Admin is locked" or "Missing IAP
assertion" to everyone. That is correct: it stays shut until step 4.

### Let the app sign upload links

App Engine's service account has no private key, so Bucketcast signs upload
links through Google's IAM API. Enable that API and let the service account
sign as itself:

```bash
gcloud services enable iamcredentials.googleapis.com
```

```bash
gcloud iam service-accounts add-iam-policy-binding PROJECT_ID@appspot.gserviceaccount.com --member="serviceAccount:PROJECT_ID@appspot.gserviceaccount.com" --role="roles/iam.serviceAccountTokenCreator"
```

The same service account needs to read and write the bucket. New projects
normally grant it broad access already; if uploads or saves fail with a
permission error, grant it *Storage Object Admin* on the bucket.

## 3. Set up Google sign-in (OAuth) for a personal project

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
   ID and secret.
3. Open the client again and add this under **Authorised redirect URIs**,
   with your client ID in place of `CLIENT_ID`:

   ```
   https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect
   ```

**Unconfirmed:** whether an External app left in "Testing" needs your account
added as a test user before you can sign in. If sign-in is refused, add
yourself under **Audience → Test users**.

## 4. Turn on Identity-Aware Proxy

IAP is switched on for the whole App Engine app, then access is granted per
service.

Enable it (leave out the two `--oauth2` flags if you skipped step 3):

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

The admin is guarded three times over, and all three must pass: IAP's login,
the app's own check of IAP's signed token, and `BUCKETCAST_ADMIN_EMAILS`. So
if this step is misconfigured, the failure is a locked admin or a
login-walled public site, never an open admin.

## 5. Check it works

Work through these in order. Each one exercises something that has only
been tested locally so far.

- [ ] `https://PROJECT_ID.REGION.r.appspot.com/healthz` answers `{"ok": true}`
      with no login (public service is public).
- [ ] The admin address asks for a Google sign-in, lets your account in, and
      shows your email in the top corner.
- [ ] A different Google account is refused.
- [ ] Create a show with artwork. The artwork displays (bucket writes and
      public reads work).
- [ ] Upload a large episode, ideally 150 MB or more. The progress bar
      completes and the episode saves (signed upload links and CORS work).
      If the upload fails immediately, re-check the origin in `cors.json`.
- [ ] Upload an asset, insert it into an episode's notes, delete another
      asset (bucket listing and deletion work).
- [ ] Open the show page and feed links from the Episodes page. Audio plays.
- [ ] Run the feed address through a validator such as
      [Podbase](https://podba.se/validate/) or Cast Feed Validator.

## 6. Use your own domain

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
means, but check that `https://yourdomain.com/admin/` returns "Not Found".

Afterwards:

- [ ] `https://yourdomain.com/` shows the show page and
      `https://yourdomain.com/feed.xml` the feed.
- [ ] A second show on a second domain shows its own page, not the first's.
- [ ] Rebuild the show (Episodes page → Rebuild) so its feed advertises the
      new address.

## 7. Download stats in BigQuery (optional)

Create a dataset and a sink that copies Bucketcast's download log lines
into it. The dataset must exist before the sink.

```bash
bq mk --dataset PROJECT_ID:bucketcast_logs
```

```bash
gcloud logging sinks create bucketcast-downloads bigquery.googleapis.com/projects/PROJECT_ID/datasets/bucketcast_logs --log-filter="jsonPayload.event=\"bucketcast.download\"" --use-partitioned-tables
```

Give the sink permission to write. Find its identity:

```bash
gcloud logging sinks describe bucketcast-downloads --format="value(writerIdentity)"
```

and grant that identity **BigQuery Data Editor** on the `bucketcast_logs`
dataset (console: BigQuery → the dataset → Sharing → Permissions). Give the
app's service account (`PROJECT_ID@appspot.gserviceaccount.com`) **BigQuery
Job User** on the project and **BigQuery Data Viewer** on the dataset.

Turn on **Count downloads** for a show, rebuild it, and play an episode
through the show page. Within about a minute a table appears:

```bash
bq ls bucketcast_logs
```

**Unconfirmed:** the table's name. By Google's naming rule it should be
`appengine_googleapis_com_stdout`, but use whatever `bq ls` shows. Put it in
both `app.yaml` and `admin.yaml` as
`BUCKETCAST_STATS_BQ_TABLE: "PROJECT_ID.bucketcast_logs.TABLE"` and deploy
again.

- [ ] The show's Stats page shows the download you just made. If it shows an
      error instead, the app's logs have the reason; the likeliest causes
      are the table name or a missing BigQuery permission.

Counting puts the app in the download path. If you'd rather downloads keep
working even when the app is down, leave it off; audio then comes straight
from the bucket and there are no stats.

## 8. Bring over an existing show

With `BUCKETCAST_BUCKET` set in your shell, the importer writes straight to
the bucket. See the README's *Importing an existing show* and *Moving a show
from another host* for the full procedure.

PowerShell:

```powershell
$env:BUCKETCAST_BUCKET = "BUCKET"
```

```bash
python -m bucketcast import --show my-show --feed https://example.com/old/rss --dry-run
```

This uses your own Google credentials, so first run
`gcloud auth application-default login`.

- [ ] After a real import, the new feed lists the same GUIDs as the old one.

## Updating

After pulling new code, deploy both services again:

```bash
gcloud app deploy app.yaml admin.yaml
```

Your edits to `app.yaml`, `admin.yaml` and `cors.json` contain your project's
settings. Keep them out of commits you share, or keep a private copy.

## Costs

Both services scale to zero, and the bucket, App Engine, logging and
BigQuery all have free tiers that a small show should stay inside. The
usual first cost is bucket egress once downloads pass about 100 GB a month.
Set a [budget alert](https://cloud.google.com/billing/docs/how-to/budgets)
so a surprise can't go unnoticed.

## If something goes wrong

| Symptom | Likely cause |
|---|---|
| Admin says "Admin is locked" | `BUCKETCAST_IAP_AUDIENCE` or `BUCKETCAST_ADMIN_EMAILS` is empty in `admin.yaml` |
| Admin says "Invalid IAP assertion" | the audience has the wrong project number or ID |
| Admin says "not allowed" | you're signed in with an account not in `BUCKETCAST_ADMIN_EMAILS` |
| Public pages ask for a Google login | step 4's `allUsers` grant on the `default` service is missing |
| Upload fails at 0% | `cors.json` origin doesn't exactly match the admin address |
| "Could not start the upload" | the signing permission in step 2 is missing |
| Artwork and audio show as broken | the `shows/` folder isn't public (step 1) |
| Show page links point at `localhost` or the admin address | `BUCKETCAST_PUBLIC_BASE_URL` isn't set in `admin.yaml`; set it, deploy, and Rebuild |

Logs for both services:

```bash
gcloud app logs tail
```

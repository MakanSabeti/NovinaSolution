# Production setup

The pilot-scale stack, roughly $7–15/month in total:

| # | Piece | Service | Cost | Status in code |
|---|-------|---------|------|----------------|
| 1 | Database | Supabase Postgres | $0 | Done (`storage.py`, `migrations/`) |
| 2 | Auth + multi-tenant isolation | Supabase Auth | $0 | Done (`auth.py`, `migrations/002_auth_orgs.sql`, sign-in screen) |
| 3 | Hosting | Render (or Railway) | ~$7/mo | Done (`render.yaml`, `railway.json`, `Procfile`, `gunicorn.conf.py`) |
| 4 | File storage | Supabase Storage | $0–5/mo | Done (`filestore.py`, `POST /api/uploads/sign`) |
| 5 | Email | Resend | $0 | Done (`risk.send_alert_email`, SMTP kept as fallback) |
| 6 | Background jobs | none yet | $0 | Deliberately deferred (see below) |
| 7 | Error tracking | Sentry | $0 | Done (`sentry_setup.py`) |
| 8 | CI/CD | GitHub Actions → Render | $0 | Done (`.github/workflows/ci.yml`) |

The code is finished. What's left are the dashboard steps below, which only you
can do because they need your accounts.

---

## 0. Rotate the leaked credentials first

The `.env.example` in the zip you uploaded had your **real Supabase database
password** and a **real Slack webhook URL** in it. They were removed before
anything was committed here, but that file may have been shared elsewhere.
Rotate both:

- Supabase → Project Settings → Database → **Reset database password**, then
  update `DATABASE_URL` wherever you use it.
- Slack → api.slack.com/apps → your app → Incoming Webhooks → remove that
  webhook and create a new one.

## 2. Supabase Auth

1. **Project Settings → API**: copy the *Project URL* (`SUPABASE_URL`), the
   *anon / publishable* key (`SUPABASE_ANON_KEY`), and the *service_role /
   secret* key (`SUPABASE_SERVICE_ROLE_KEY`, used for Storage; keep it on the server only).
2. **Authentication → URL Configuration**: set *Site URL* to your Render URL
   (e.g. `https://stratum.onrender.com`) and add `http://localhost:5001` under
   *Redirect URLs*. Confirmation and magic-link emails send people back there.
3. **Authentication → Sign In / Providers → Email**: keep *Confirm email* on.
   Supabase's built-in mailer is limited to a few emails per hour, so point it
   at Resend (step 5): **Authentication → Emails → SMTP Settings**, host
   `smtp.resend.com`, port `465`, user `resend`, password = your Resend API key.
4. JWT verification needs no extra setup on new projects: the backend fetches the
   project's public signing keys from `SUPABASE_URL/auth/v1/.well-known/jwks.json`.
   Only if your project still uses the **legacy JWT secret** (Project Settings → JWT Keys),
   set `SUPABASE_JWT_SECRET` to it.
5. Set `BOOTSTRAP_ADMIN_EMAIL` to your own email. The first time you sign in, you
   become owner of the original *Default Organization* and keep every site created
   before auth existed. Set it **before** your first sign-in. If you sign in first,
   you get a new, empty organization instead.

**How tenancy works.** Every user belongs to an organization, and every site
query is filtered by the caller's org. Another org's site id returns the same
404 as a site that doesn't exist. A new user who signs up gets their own
organization. To add a teammate to *yours*, an owner/admin invites their email
(`POST /api/org/invites {"email": "..."}`). They join your org the first time
they sign in with that address. All tables keep RLS on with no policies, so
Supabase's auto-generated REST API can't read them. Only this backend can.

## 3. Hosting on Render

1. Push this repo to GitHub (done).
2. Render → **New → Blueprint** → pick the repo. Render reads `render.yaml`,
   creates a *Starter* ($7/mo) web service in Oregon (the same region as your
   Supabase project), and asks for each secret.
3. Fill in the values from `backend/.env.example`. `APP_ENV=production` is
   already set, and with it the app refuses to start if `AUTH_DISABLED` is on.
4. Deploys run `gunicorn` (2 workers × 4 threads, 120 s timeout for photo
   analysis) behind Render's HTTPS load balancer. The same service serves the
   frontend at `/`, so there's one URL and no CORS setup.

**Railway instead:** New Project → Deploy from GitHub repo. `railway.json`
supplies the build/start commands and health check. Add the same environment
variables and set `APP_ENV=production`.

## 4. File storage (Supabase Storage)

No dashboard work needed. With `SUPABASE_SERVICE_ROLE_KEY` set, the backend
creates a private `stratum-files` bucket on first use.

- **How uploads work.** The browser asks `POST /api/uploads/sign` for a
  short-lived upload URL, PUTs the file straight to Supabase (it never passes
  through the web server), then sends `{"storage_path": "org_12/dem/…json"}` in
  place of the inline data. This works for every DEM/GeoJSON field on the
  volumetric, stockpile, cross-section, risk-check, site and capture endpoints,
  and for `image_path` on the photo endpoints.
- The frontend does this automatically for DEM/capture files over 4 MB. Smaller
  files still go inline, exactly as before.
- **Isolation.** Paths are always `org_<id>/…`. The backend only issues upload
  URLs inside the caller's prefix and refuses to read outside it.
- Photo-mode captures keep their source photo in Storage, so a vision score can be
  audited later. Get a link with `GET /api/files/url?path=...`.
- **Limits.** The free tier allows 50 MB per file and 1 GB in total. Point clouds
  bigger than 50 MB need the Pro plan, or R2 (below).
- **Cloudflare R2 later.** R2 has no egress fees, which matters once customers
  repeatedly download heavy imagery. The Storage calls are all in
  `filestore.py`, behind five small functions (`create_signed_upload`,
  `upload_bytes`, `download`, `signed_download_url`, `ensure_bucket`), so
  switching means replacing that one file.

## 5. Email (Resend)

1. resend.com → **Domains → Add domain**, then add the DNS records it shows.
   This is what gets alerts delivered instead of landing in spam.
2. **API Keys → Create** → set `RESEND_API_KEY`. Set `ALERT_FROM_EMAIL` to an
   address on that domain, e.g. `Stratum Alerts <alerts@yourdomain.com>`.
3. **Who gets alerts:** each site's *Email recipients* field (Risk & Compliance
   tab). If that's blank, alerts go to everyone in the site's organization.
   Alerts never go to a global address, so one customer's alerts can't reach
   another customer. Slack webhooks can also be set per site.
4. "Send test notification" sends a test to *you* (the signed-in user).

SMTP still works as a fallback when `RESEND_API_KEY` is empty.

## 6. Background jobs: deferred on purpose

Processing stays synchronous. Gunicorn's 120 s timeout covers photo analysis,
and big inputs no longer bloat requests because they come from Storage. Add a
queue only when one specific operation is *measured* as too slow. Sentry's
performance view will show that if you raise `SENTRY_TRACES_SAMPLE_RATE` to
e.g. `0.1`. At that point, use Upstash Redis (serverless, per-request billing)
with RQ or Celery, starting with photo analysis and large-DEM processing.

## 7. Sentry

sentry.io → create a **Flask** project → copy the DSN into `SENTRY_DSN` on
Render. That's all. Errors are tagged with the Supabase user id and org id.
Request bodies and PII are not sent. With `SENTRY_DSN` unset (local, CI),
nothing is reported. The default sends errors only, which fits well inside the
free 5k/month.

## 8. CI/CD

`.github/workflows/ci.yml` runs on every push to `main` and on every PR:

- `ruff` lint (real bugs only: syntax errors, undefined names, unused imports)
- `pytest`: the auth and tenant-isolation tests (a second org gets 404 on every
  site route, bad/expired/forged tokens are rejected, the invite flow, storage
  path confinement), plus the Resend and Storage clients
- the existing Postgres smoke test, against a throwaway Postgres 16 container
- a boot check: gunicorn starts, `/` serves the page, and `/api/sites` returns 401 without a token
- a syntax check of the frontend's inline JavaScript

`render.yaml` sets `autoDeployTrigger: checksPass`, so Render only deploys
commits on `main` whose CI passed. Recommended: GitHub → Settings → Branches →
protect `main` and require the *CI* checks before merging.

## Running locally

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in DATABASE_URL + SUPABASE_* at minimum
python app.py               # http://localhost:5001 (serves the frontend too)
```

To skip sign-in while developing, put `AUTH_DISABLED=true` in `.env`.

Tests need a throwaway Postgres (never your Supabase project):

```bash
docker run -d --name stratum-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=stratum_test -p 5432:5432 postgres:16
pytest -q
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/stratum_test python tests/smoke_test_postgres.py
```

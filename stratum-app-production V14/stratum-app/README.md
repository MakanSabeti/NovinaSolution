# Stratum — Site Progress MVP

Two separate services:

- **`frontend/`** — a single static HTML page. Upload UI, dashboard,
  blueprint-vs-as-built map. No analysis logic lives here — it only calls
  the backend over HTTP and renders whatever JSON comes back.
- **`backend/`** — a Python (Flask) API that does the actual analysis:
  exact polygon-overlap math for Measured mode, and a real vision-model call
  for Photo mode. This is where "the image recognition" actually happens.

## Run it

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # DATABASE_URL + SUPABASE_* (see DEPLOYMENT.md); ANTHROPIC_API_KEY for Photo mode
python app.py
```

Open `http://localhost:5001`. The backend serves the frontend too, so there's no
second server and no CORS setup. You'll get a sign-in screen (Supabase Auth);
create an account and you land in your own organization. Set
`AUTH_DISABLED=true` in `.env` to skip sign-in during local development.

The frontend can still be served on its own (`cd frontend && python3 -m
http.server 8080`). If you do that, point the sidebar's "Backend" panel at
`http://localhost:5001`.

**Production:** see [`DEPLOYMENT.md`](DEPLOYMENT.md). It covers Supabase Auth
and Storage, Render/Railway hosting, Resend email, Sentry, and GitHub Actions CI.

## How the modes/views differ

The app has two tabs now:

**Single Capture** — analyze one point in time, no history kept.

| | Measured | Photo (AI vision) |
|---|---|---|
| Input | two GeoJSON files (planned + as-built) | one GeoJSON (planned) + one real photo |
| Backend logic | `backend/geometry.py` — polygon clipping/area math | `backend/vision.py` — calls Claude with the image |
| Precision | exact, survey-grade if your footprints are accurate | an estimate — a visual judgment call, not a measurement |
| Needs an API key? | no | yes (`ANTHROPIC_API_KEY` in `backend/.env`) |

**Timeline** — create a persistent site, add captures over time, and get
trend analytics: velocity (%/week), a forecasted completion day, automatic
stall detection (a feature with zero movement between two captures), and
schedule variance against an optional `target_day` set per blueprint
feature. Click "Load demo timeline" for a worked example — a site tracked
across day 5/15/25 where one feature (the stockpile pad) is correctly
flagged as both stalled and behind schedule while everything else finishes
on time.

The **Project schedule** panel — visible as soon as a site exists, before
any captures — is the primary way to set this: every feature gets its own
row of 10 milestone boxes (10% by day _, 20% by day _, ... 100% by day _),
all editable at once, saved with a single click. This replaced an earlier
single-target-day version of this panel, which didn't match what was asked
for and caused real confusion — it's gone now, there's one schedule system,
not two.

**Click any row in the feature trend table** for a focused per-feature view:
that feature's metadata, its own progress chart (with the actual piecewise
planned curve drawn through its milestones, not a straight line), and the
same 10-milestone grid, pre-filled with whatever's already saved — editable
here too. Both this popup and the batch panel read and write the exact same
underlying schedule data, confirmed by saving in one and immediately
re-opening the other.

The BEHIND SCHEDULE flag is computed by interpolating between whichever two
milestones bracket the most recent capture, so one mistyped day only throws
off its own segment instead of the whole schedule.

**Forecasting fits a logistic S-curve to the whole capture history**, not a
straight line through the last two points — construction progress has a
slow start and a slow finish, and a linear extrapolation systematically
gets both wrong. Verified against synthetic data generated from a known
S-curve: the fit recovers the true parameters to within 1e-9, and on a
realistic slow-start scenario it landed within floating-point precision of
the true completion date, versus a 5.4-day error for the old linear method
on the same data. Feature and site-level charts draw the actual fitted
curve, not a straight dashed line.

**Set the schedule from Excel instead of typing it in.** Give the site a
start date, then click "Download Excel template" — it comes back pre-filled
with every feature as a row and a single **Expected Finish Date** column.
Fill in the one date you actually know per feature (a real calendar date,
not a day-offset) and upload it back. That single date is automatically
spread into the same 10%-100% pace schedule the manual grid produces —
evenly spaced between the site's start date and the finish date you gave —
so behind-schedule detection and the forecast work exactly the same way,
without typing ten dates per feature. Rows that don't match a feature, or
give a finish date on or before the project start, are called out as
warnings rather than silently dropped or silently accepted.

> **Fixed bug:** adding a capture for a day that already has one used to
> create a duplicate row instead of replacing it, which silently broke
> velocity/forecast for every feature (two points at the same day = zero
> time delta = no rate to compute) and made the chart show what looked like
> a single stuck point. Re-adding a day now replaces that day's capture
> instead of duplicating it, and the "Add capture" button now disables
> itself and shows a confirmation while the request is in flight so
> accidental double-clicks can't reintroduce the same problem.

**Earthwork** — real elevation/volume analysis, not just footprint area.
Needs three DEM (elevation grid) surfaces: baseline (original ground),
design (target grade), and as-built (latest survey). Computes:
- **Volumetric % complete** — earth actually moved vs. total planned volume.
  Stricter than footprint completion: a pad can look 100% "done" by outline
  while still sitting a meter short of design elevation.
- **Remaining cut/fill** — how much more earthwork is needed to hit design grade.
- **Stockpile tonnage** — volume above a base elevation, constrained to the
  stockpile's footprint polygon, converted to tons via a material density
  preset, and reconciled against an ERP/recorded tonnage figure (flagged if
  the variance exceeds tolerance).
- **Cross-sections** — an elevation profile along any line, design vs. as-built.

Click "Load demo earthwork data" for a worked example on the same Hollow
Creek site: 86% volumetric complete (vs. 71.4% by footprint on the same
day), and a stockpile that drone measurement puts at ~15,800 tons against a
recorded 9,000 tons — flagged as a real discrepancy worth investigating.

**Risk & Compliance** — automated risk flags, compliance checks, and
configurable alerts, all backed by real geometry/elevation math rather than
labels:
- **Standing water in non-pond zones** — any grid cell at or below a water
  threshold elevation that falls outside every designated pond zone gets
  flagged, grouped into distinct patches (connected-component clustering
  over the grid, filtering out single-cell noise).
- **Boundary encroachment / buffer intrusion** — upload a permit/property
  boundary and any protected buffer zones (stream setbacks, vegetation
  clearance limits) as GeoJSON. Any as-built footprint that extends past the
  boundary, or into a buffer zone, gets flagged with the exact intrusion
  area (the same polygon-clip math as Measured mode).
- **Retention pond capacity** — real integrated volume between the current
  pond surface and its rim/spillway elevation, flagged if it's under a
  configured minimum.
- **Erosion** — compares two elevation surveys and flags unexpected material
  loss *outside* active work zones (areas that should be stable but aren't).
- **Configurable pace alerts** — flag any feature more than a set number of
  points behind its planned pace (reusing the Timeline tab's schedule
  variance), plus real Slack webhook / SMTP email delivery if configured in
  `backend/.env` (see `.env.example`). Alerts always persist to a per-site
  log regardless of whether external delivery is configured — check the
  Alert log table any time, not just when a notification arrives.

Click "Load demo risk scenario" for a worked example: a pad that grades
past the permit boundary (400 m² encroachment), a road that intrudes 750 m²
into a stream setback, a 225 m² standing-water patch outside the pond, and
a pond running 1,325 m³ under its minimum required capacity — four
independent, correctly-triggered flags from one synthetic site.

**Reports** — automated executive reporting. Pick a site and a look-back
period, click "Generate report," and get a written summary plus a feature
table plus recent alerts — built entirely from numbers the Timeline and
Risk & Compliance tabs already compute, not a new analysis. Every figure is
deterministic (same data always produces the same report), and "Download
PDF" gets the same content as a real, shareable file. This is the delivery
mechanism for everything else in the app: instead of someone having to open
the dashboard and check each tab, the weekly status is one generated
document.

## Project layout

```
stratum-app/
├── backend/
│   ├── app.py           Flask routes
│   ├── geometry.py       Measured-mode math (no network)
│   ├── vision.py         Photo-mode analysis (calls Anthropic API)
│   ├── storage.py        Postgres/Supabase persistence (sites, captures, zones, alerts)
│   ├── temporal.py       Timeline trend analytics
│   ├── volumetrics.py    Earthwork/volumetric analysis
│   ├── risk.py           Risk, compliance & anomaly detection
│   ├── auth.py           Supabase Auth JWT verification + org resolution
│   ├── filestore.py      Supabase Storage (large DEM / photo uploads)
│   ├── sentry_setup.py   Error tracking (no-op without SENTRY_DSN)
│   ├── gunicorn.conf.py  Production WSGI server settings
│   ├── tests/            pytest suite + Postgres smoke test
│   ├── requirements.txt
│   ├── .env.example
│   └── README.md
└── frontend/
    ├── site_progress_mvp.html
    └── sample-data/
        ├── blueprint.geojson
        └── day15_capture.geojson
```

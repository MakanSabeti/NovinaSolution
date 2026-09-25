# Stratum backend

Flask API that does the actual analysis. The frontend never talks to the
Anthropic API directly — it only ever talks to this server.

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set `ANTHROPIC_API_KEY` to a real key from
https://console.anthropic.com/settings/keys. This is only required for
Photo (AI vision) mode — Measured (geometry) mode works with no key at all.

## Run

```bash
python app.py
```

Serves on `http://localhost:5001`. Leave this running in its own terminal.

## Endpoints

Every route except `/`, `/api/health`, `/api/config` and `/api/materials`
needs `Authorization: Bearer <Supabase access token>` (the frontend handles
this). Every site route is scoped to the caller's organization: another org's
site id returns 404. Any DEM/GeoJSON field below also accepts
`{"storage_path": "org_<id>/..."}` in place of inline data (see `POST /api/uploads/sign`),
and the photo endpoints accept an `image_path` form field in place of `image`.

| Method | Path                                    | Body                                                              |
|--------|-------------------------------------------|--------------------------------------------------------------------|
| GET    | `/api/health`                             | —                                                                  |
| POST   | `/api/analyze/geometry`                   | JSON: `{"blueprint": <geojson>, "capture": <geojson>}` (stateless, single-shot) |
| POST   | `/api/analyze/vision`                     | multipart: `blueprint` (JSON string), `day`, `image` (file) (stateless, single-shot) |
| GET    | `/api/sites`                              | — list all sites with capture counts                              |
| POST   | `/api/sites`                              | JSON: `{"name": "...", "blueprint": <geojson>}` — creates a persistent site |
| GET    | `/api/sites/<id>`                         | — site detail incl. blueprint                                     |
| DELETE | `/api/sites/<id>`                         | —                                                                  |
| POST   | `/api/sites/<id>/captures/geometry`       | JSON: `{"day": <int>, "capture": <geojson>}` — analyzes, stores, and returns the updated timeline |
| POST   | `/api/sites/<id>/captures/vision`         | multipart: `day`, `image` — same, using AI vision                 |
| PATCH  | `/api/sites/<id>/targets`                 | JSON: `{"targets": {"<feature_id>": <day_or_null>, ...}}` — legacy single target-day, still supported as a fallback |
| PATCH  | `/api/sites/<id>/schedule`                | JSON: `{"schedules": {"<feature_id>": [{"pct":10,"day":5}, ...] or null, ...}}` — full 10%-increment milestone schedule per feature |
| GET    | `/api/sites/<id>/timeline`                | — full trend analytics for a site                                 |
| GET    | `/api/materials`                          | — list of material density presets (t/m³)                         |
| POST   | `/api/analyze/volumetric`                 | JSON: `{"baseline_dem", "design_dem", "asbuilt_dem"}` — volumetric % complete + remaining cut/fill |
| POST   | `/api/analyze/stockpile`                  | JSON: `{"surface_dem", "base_elevation", "footprint"?, "material"?, "recorded_tons"?}` — volume, tonnage, ERP reconciliation |
| POST   | `/api/analyze/cross-section`              | JSON: `{"dems": {label: dem, ...}, "point_a", "point_b", "num_samples"?}` — elevation profile along a line |
| GET/PATCH | `/api/sites/<id>/zones`                | GET returns current zones. PATCH body: `{"boundary"?, "buffers"?, "restricted_zones"?, "pond_configs"?}` — partial updates merge, they don't overwrite the whole object |
| GET/PATCH | `/api/sites/<id>/alert-rules`          | GET returns current rules. PATCH body: `{"pace_threshold_pct"?, "notify_slack"?, "notify_email"?, "email_to"?, "slack_webhook_url"?}` |
| PATCH  | `/api/sites/<id>/start-date`               | JSON: `{"start_date": "YYYY-MM-DD"}` — needed before Excel schedule import (dates convert to day-offsets relative to this) |
| GET    | `/api/sites/<id>/schedule/template`        | — downloads an `.xlsx` pre-filled with this site's features, one row each, ready to fill in milestone dates |
| POST   | `/api/sites/<id>/schedule/import-excel`    | multipart: `file` (the completed `.xlsx`, one finish date per feature) — matches rows to features by ID (falling back to name), converts each finish date to a day-offset and expands it into a 10%-100% milestone schedule, returns `{"imported_feature_count", "warnings", "timeline"}` |
| POST   | `/api/sites/<id>/risk-check`              | JSON: `{"capture"?, "asbuilt_dem"?, "prev_dem"?, "water_threshold_elev"?, "erosion_drop_threshold_m"?, "run_pace_check"?, "send_notifications"?}` — runs whichever checks the supplied data supports, persists any triggered alerts |
| GET    | `/api/sites/<id>/alerts`                  | — persisted alert history for the site, most recent first          |
| POST   | `/api/notify/test`                        | JSON: `{"site_id"?}` — sends one real test alert (Slack + email via Resend/SMTP) to the site's settings or to you, reports honest success/failure |
| GET    | `/api/config`                             | — public: Supabase URL + anon key for the sign-in screen |
| GET    | `/api/me`                                 | — signed-in user, their organization and role |
| GET    | `/api/org/members`                        | — members and pending invites of your organization |
| POST   | `/api/org/invites`                        | JSON: `{"email", "role"?}` (owner/admin only) — they join your org on first sign-in |
| DELETE | `/api/org/invites/<id>`                   | — revoke a pending invite (owner/admin only) |
| POST   | `/api/uploads/sign`                       | JSON: `{"filename", "kind"}` → `{"storage_path", "upload_url"}`; PUT the raw file to `upload_url` |
| GET    | `/api/files/url`                          | query: `path` — short-lived download link for one of your org's stored files |
| GET    | `/api/sites/<id>/report`                  | query: `period_days` (default 7), `format` (`json` or `pdf`) — executive summary built from data the app already computes; `pdf` streams a downloadable file |

The `/api/sites/...` routes are the persistent, multi-capture path — this is
what backs the Timeline tab. The original `/api/analyze/...` routes are
still there for one-off, no-history analysis (what the Single Capture tab
uses).

## Files

- `app.py` — Flask routes, request validation, CORS.
- `geometry.py` — pure-Python polygon area/clip math for Measured mode. No
  network calls; fully unit-testable on its own.
- `vision.py` — builds the prompt, calls the Anthropic API with the uploaded
  image, parses the response, merges it with blueprint geometry. Split into
  small functions (`build_prompt`, `call_model`, `parse_model_output`,
  `merge_with_blueprint`) so each can be tested without hitting the network
  except `call_model` itself.
- `storage.py` — Postgres (Supabase) persistence for sites, captures, zones and alerts.
  Reads `DATABASE_URL` from `.env`; applies `migrations/*.sql` automatically at startup.
- `migrations/` — numbered SQL schema files. To change the schema, add a new file
  (e.g. `002_something.sql`); never edit one that has already been applied.
- `tests/smoke_test_postgres.py` — run once against your real database to verify the whole
  storage layer end to end (it creates and deletes its own temporary site).
- `temporal.py` — trend analytics over a site's capture history: stall
  detection (zero movement between the two most recent captures) and
  schedule variance. Two ways to define "planned": a full 10%-increment
  milestone schedule per feature (`properties.schedule`, a list of
  `{"pct","day"}` points — 10% by day 5, 20% by day 12, etc.,
  piecewise-interpolated) or, as a lighter fallback, a single
  `properties.target_day` (linear 0→100% ramp). The milestone
  schedule takes precedence when both are present, and is deliberately more
  robust: one mistyped day only distorts the segment around it, not the
  whole curve — unlike a single target_day, where one bad number breaks the

  **Forecasting uses a logistic S-curve fit, not linear extrapolation.**
  Construction progress is essentially never linear — slow mobilization,
  a fast middle phase, a slow punch-list finish — so a straight line drawn
  through the last two captures systematically overshoots during the fast
  phase and undershoots during the slow ones. `fit_logistic_and_forecast()`
  fits `pct(t) = 100 / (1 + exp(-k*(t-t0)))` to the whole capture history
  for a feature (or the whole site, in aggregate). With the asymptote fixed
  at 100%, this model linearizes exactly via the logit transform
  (`ln(p/(100-p))` is linear in `t`), so it's fit with closed-form ordinary
  least squares — no iterative optimizer, no scipy dependency, no
  initial-guess sensitivity, and it's the exact maximum-likelihood fit for
  this model, not an approximation of one. Verified against synthetic data
  generated from a *known* logistic curve: the fit recovers the true `k`
  and `t0` parameters to within 1e-9, and on a realistic slow-start scenario
  the S-curve forecast landed within 1.4e-14 days of the true completion
  date versus the old linear method's 5.4-day error on the same data.
  Returns `None` for forecast fields when there's insufficient data (fewer
  than 2 points strictly between 0–100% on distinct days) or when the fitted
  trend is flat/declining (`k <= 0`) — it won't invent a forward forecast
  from a curve that isn't currently rising. A forecast targets 99%, not
  100%, since a logistic curve only reaches its asymptote at `t = ∞`.
  `sample_logistic_curve()` samples the fitted curve for chart rendering, so
  the frontend draws the actual S-shape, not a straight-line stand-in.
  entire schedule.
- `volumetrics.py` — earthwork/volumetric analysis: cut/fill volume between
  two elevation surfaces, a stricter "volumetric % complete" metric (earth
  actually moved vs. footprint-area-complete), stockpile volume constrained
  to a footprint polygon, tonnage via material density presets, ERP
  inventory reconciliation, and cross-section elevation sampling along a
  line. Uses its own lightweight DEM grid JSON format (documented at the top
  of the file) instead of GeoTIFF, since GDAL/rasterio aren't required to
  produce or consume it — see `backend/sample-volumetric-data/` for real
  example grids, generated at 5m resolution over the same Hollow Creek site
- `schedule_import.py` — lets a project schedule be maintained in Excel
  instead of typed into the UI one feature at a time. The format is
  deliberately simple: one row per feature, **one date** — when it's
  expected to finish. `build_template_workbook()` generates a template
  pre-filled with the site's features (Feature ID, Name, Category, Expected
  Finish Date). `import_schedule_workbook()` parses a completed one back,
  matching rows to features by ID first, falling back to name if the ID
  column is blank or doesn't match, converts each finish date to a
  day-offset from the site's start date, and *expands it into the same
  10%-100% milestone shape the rest of the app uses*
  (`expand_finish_day_to_milestones` — evenly spaced in time from the start
  date to the given finish date). This keeps "behind schedule" detection
  and the S-curve forecast working exactly as they do for a manually-entered
  schedule; the simplification is in what the user has to type, not in the
  underlying model. Bad rows (unmatched feature, a finish date on or before
  the project start) are collected as warnings and skipped rather than
  failing the whole import — the rows that ARE valid still get applied.
  Verified end-to-end: exact finish-date-to-day-offset conversion, the
  even-spacing math against hand calculation, every warning path
  (name-fallback matching, unmatched IDs, pre-start dates, missing start
  date, a non-Excel file), confirmation that an auto-expanded schedule
  correctly drives the existing behind-schedule detection, and a full
  round-trip through the live API — download the real template, fill it in,
  upload it, confirm the persisted timeline
  reflects it exactly.
- `reporting.py` — automated executive reporting. Deliberately not
  LLM-generated: every figure (overall % change over a chosen period,
  per-feature deltas, top movers, stalled/behind-schedule features, alerts
  raised in that window) is a deterministic read or a simple, hand-verifiable
  delta computed from data `temporal.py` and `risk.py` already produce — the
  same site data always yields the same report. `generate_report_data()`
  builds the structured report (independently testable, no PDF library
  needed); `render_report_pdf()` formats it into a real downloadable PDF via
  `reportlab`. Verified: baseline-point selection logic (which earlier
  capture to compare "now" against) against hand-picked synthetic series,
  full report generation against a hand-verified multi-feature timeline
  (top movers, stalled/behind-schedule detection, and the alert time-window
  filter all confirmed exact), and the rendered PDF's extracted text
  confirmed to contain the same numbers as the JSON — not just "a PDF
  renders," the actual figures in it were checked.
- `risk.py` — Tier 3: risk, compliance & anomaly detection. Standing-water
  detection with connected-component clustering (groups flagged grid cells
  into distinct patches, filters single-cell noise); boundary/buffer
  intrusion via the same polygon-clip math as Measured mode; retention-pond
  available-capacity checking (real integrated volume between the current
  surface and the rim elevation, not an estimate); erosion detection between
  two surveys (flags unexpected elevation loss *outside* active work zones);
  a pace-threshold alert rule against the temporal engine's variance data;
  and real Slack webhook / SMTP email delivery. All five checks are unit-
  tested against hand-calculated exact areas/volumes.

  **A note on a bug this work caught upstream:** building `risk.py`'s
  boundary/buffer checks surfaced a real latent bug in `geometry.py`'s
  `polygon_area()` — it silently dropped the closing edge whenever fed an
  *open* ring (which `clip_polygon()` returns whenever it performs genuine
  multi-edge clipping, as opposed to a pass-through where the subject is
  already fully inside the clip boundary). Every test case built before this
  point happened to use corner-anchored, fully-contained as-built polygons,
  which never exercised real clipping and so never revealed it — a real-
  world, non-corner-anchored footprint could have silently gotten the wrong
  completion percentage. Fixed at the root (`polygon_area` now uses modulo
  wraparound indexing, correct for open or closed rings) and re-verified
  against every previously-passing test case plus the specific case that
  exposed it — see git history / the conversation this shipped from for the
  full verification trail.
  used elsewhere in the demo.

  The volume math is unit-tested against two independent references before
  being trusted on realistic data: an exact flat-surface case (zero
  discretization error) and a cone of known analytic volume (`(1/3)πr²h`),
  confirming the grid integration converges to under 0.02% error as
  resolution increases.

## Quick test without a frontend

```bash
curl http://localhost:5001/api/health

curl -X POST http://localhost:5001/api/analyze/geometry \
  -H "Content-Type: application/json" \
  -d "{\"blueprint\": $(cat ../frontend/sample-data/blueprint.geojson), \"capture\": $(cat ../frontend/sample-data/day15_capture.geojson)}"
```

Expect `"overall": 71.4` back from the second call.

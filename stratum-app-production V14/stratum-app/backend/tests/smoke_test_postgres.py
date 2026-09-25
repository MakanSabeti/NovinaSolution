"""
smoke_test_postgres.py

Run this ONCE against your real Supabase project after migrating, to prove the
Postgres storage layer and every API route that touches it behave like the SQLite
version did. It creates a temporary site (name starts with __stratum_smoke_test__),
exercises everything, and deletes it in a `finally` block -- it does not touch any
other data.

Usage (from the backend/ folder, venv active, DATABASE_URL set in .env):
    python tests/smoke_test_postgres.py

Exits 0 and prints "ALL CHECKS PASSED" on success; exits 1 on the first failure.
"""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

# This test exercises the storage layer and routes, not authentication (that's
# tests/test_auth_tenancy.py), so it runs as the Default Organization.
os.environ["AUTH_DISABLED"] = "1"

if not os.environ.get("DATABASE_URL"):
    sys.exit("DATABASE_URL is not set (put it in backend/.env). Aborting.")

import storage  # noqa: E402
import app as stratum_app  # noqa: E402  (importing runs init_db(), i.e. applies migrations)

HERE = os.path.dirname(__file__)
SAMPLE = os.path.join(HERE, "..", "sample-timeline-data")
client = stratum_app.app.test_client()
passed = 0


def check(label, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL: {label} {detail}")
        raise SystemExit(1)
    passed += 1
    print(f"  ok  {label}")


def load(name):
    with open(os.path.join(SAMPLE, name)) as f:
        return json.load(f)


site_id = None
nan_site_id = None
try:
    print("== migrations ==")
    storage.init_db()  # second call: must be a harmless no-op
    with storage.get_conn() as conn:
        names = [r["name"] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
        check("001_init.sql recorded exactly once", names.count("001_init.sql") == 1, names)
        check("002_auth_orgs.sql recorded exactly once", names.count("002_auth_orgs.sql") == 1, names)
        rls = conn.execute(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
            "('organizations','sites','captures','alerts','org_members','org_invites')"
        ).fetchall()
        check("row-level security enabled on all 6 tables", len(rls) == 6 and all(r["relrowsecurity"] for r in rls), rls)

    print("== site lifecycle via API ==")
    blueprint = load("blueprint.geojson")
    r = client.get("/api/health")
    check("health", r.status_code == 200)
    r = client.post("/api/sites", json={"name": "__stratum_smoke_test__", "blueprint": blueprint, "start_date": "2026-01-01"})
    check("create site -> 201", r.status_code == 201, r.get_data(as_text=True)[:200])
    site = r.get_json()
    site_id = site["id"]
    check("site has int id + org_id", isinstance(site_id, int) and isinstance(site.get("org_id"), int))
    check("default reference_zones", site["reference_zones"]["buffers"] == [] and site["reference_zones"]["boundary"] is None)
    check("default alert_rules", site["alert_rules"]["pace_threshold_pct"] == 10)
    check("created_at is a float epoch", isinstance(site["created_at"], float))
    check("blueprint round-trips", len(site["blueprint"]["features"]) == len(blueprint["features"]))

    print("== captures ==")
    for day in (5, 15, 25):
        r = client.post(f"/api/sites/{site_id}/captures/geometry",
                        json={"day": day, "capture": load(f"day{day:02d}_capture.geojson")})
        check(f"capture day {day} -> 201", r.status_code == 201, r.get_data(as_text=True)[:200])
    check("3 captures stored", len(storage.get_captures(site_id)) == 3)
    r = client.post(f"/api/sites/{site_id}/captures/geometry", json={"day": 15, "capture": load("day15_capture.geojson")})
    caps = storage.get_captures(site_id)
    check("re-posting day 15 replaces, not duplicates", r.status_code == 201 and len(caps) == 3, len(caps))
    check("captures ordered by day", [c["day"] for c in caps] == [5, 15, 25])
    check("capture result is a dict", isinstance(caps[0]["result"], dict))
    listed = [s for s in client.get("/api/sites").get_json() if s["id"] == site_id]
    check("list_sites shows capture_count=3", len(listed) == 1 and listed[0]["capture_count"] == 3, listed)

    print("== timeline / schedule / targets ==")
    r = client.get(f"/api/sites/{site_id}/timeline")
    check("timeline -> 200", r.status_code == 200)
    fid = blueprint["features"][0]["properties"]["id"]
    r = client.patch(f"/api/sites/{site_id}/schedule", json={"schedules": {fid: [{"pct": 50, "day": 10}, {"pct": 100, "day": 20}]}})
    check("schedule patch -> 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    stored = next(f for f in storage.get_site(site_id)["blueprint"]["features"] if f["properties"]["id"] == fid)
    check("schedule persisted on the feature", stored["properties"]["schedule"][1]["day"] == 20)
    r = client.patch(f"/api/sites/{site_id}/targets", json={"targets": {fid: 22}})
    check("targets patch -> 200", r.status_code == 200)
    stored = next(f for f in storage.get_site(site_id)["blueprint"]["features"] if f["properties"]["id"] == fid)
    check("target_day persisted", stored["properties"]["target_day"] == 22)
    check("schedule survived the later targets update", "schedule" in stored["properties"])
    r = client.patch(f"/api/sites/{site_id}/start-date", json={"start_date": "2026-02-01"})
    check("start-date patch", r.status_code == 200 and storage.get_site(site_id)["start_date"] == "2026-02-01")

    print("== zones / alert rules merge ==")
    r = client.patch(f"/api/sites/{site_id}/alert-rules", json={"pace_threshold_pct": 15})
    rules = r.get_json()
    check("alert rules merged (kept other keys)", rules["pace_threshold_pct"] == 15 and rules["notify_slack"] is False, rules)
    r = client.patch(f"/api/sites/{site_id}/zones", json={"restricted_zones": [{"id": "z1"}]})
    zones = r.get_json()
    check("zones merged (kept buffers key)", zones["restricted_zones"] == [{"id": "z1"}] and zones["buffers"] == [], zones)

    print("== concurrent read-modify-write (row locking) ==")
    keys = [f"k{i}" for i in range(8)]
    errors = []

    def worker(k):
        try:
            storage.update_alert_rules(site_id, {k: True})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    [t.start() for t in threads]
    [t.join() for t in threads]
    final = storage.get_site(site_id)["alert_rules"]
    check("no worker errored", not errors, errors)
    check("all 8 concurrent edits survived (no lost updates)", all(final.get(k) is True for k in keys), final)

    print("== alerts ==")
    ids = storage.add_alerts(site_id, [
        {"rule_type": "smoke", "severity": "info", "feature_id": fid, "message": "one"},
        {"rule_type": "smoke", "severity": "high", "message": "two", "slack_sent": True},
    ])
    check("add_alerts returns 2 ids", len(ids) == 2 and all(isinstance(i, int) for i in ids))
    alerts = storage.get_alerts(site_id)
    check("get_alerts returns them", len(alerts) == 2)
    check("slack_sent is int 0/1 like before", {a["message"]: a["slack_sent"] for a in alerts} == {"one": 0, "two": 1})
    check("alerts endpoint -> 200", client.get(f"/api/sites/{site_id}/alerts").status_code == 200)

    print("== reports ==")
    r = client.get(f"/api/sites/{site_id}/report?period_days=30")
    check("report json -> 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    r = client.get(f"/api/sites/{site_id}/report?period_days=30&format=pdf")
    check("report pdf -> valid PDF", r.status_code == 200 and r.data[:4] == b"%PDF")

    print("== risk check (pace only) ==")
    r = client.post(f"/api/sites/{site_id}/risk-check", json={"run_pace_check": True})
    check("risk-check -> 200", r.status_code == 200, r.get_data(as_text=True)[:200])

    print("== NaN / Infinity safety ==")
    nan_site_id = storage.create_site("__stratum_smoke_test__nan", blueprint)
    storage.add_capture(nan_site_id, 1, "geometry", {"pct": float("nan"), "vel": float("inf"), "ok": 1.5})
    back = storage.get_captures(nan_site_id)[0]["result"]
    check("NaN/Inf stored as null, finite values untouched", back == {"pct": None, "vel": None, "ok": 1.5}, back)

    print("== delete + cascade ==")
    r = client.delete(f"/api/sites/{site_id}")
    check("delete -> 204", r.status_code == 204)
    with storage.get_conn() as conn:
        n_caps = conn.execute("SELECT COUNT(*) AS n FROM captures WHERE site_id = %s", (site_id,)).fetchone()["n"]
        n_alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE site_id = %s", (site_id,)).fetchone()["n"]
    check("captures and alerts cascaded away (no orphans)", n_caps == 0 and n_alerts == 0, (n_caps, n_alerts))
    check("get_site now None", storage.get_site(site_id) is None)
    site_id = None

    print(f"\nALL CHECKS PASSED ({passed})")
finally:
    for sid in (site_id, nan_site_id):
        if sid is not None:
            try:
                storage.delete_site(sid)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup warning for site {sid}: {e}")
    storage.close_pool()

import json
import os

import pytest

import filestore
import storage
from conftest import make_token

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample-timeline-data")


def load(name):
    with open(os.path.join(SAMPLE, name)) as f:
        return json.load(f)


def create_site(user, name="tenant test site"):
    r = user.post("/api/sites", json={"name": name, "blueprint": load("blueprint.geojson"), "start_date": "2026-01-01"})
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()


# ---------------------------------------------------------------- authentication -----

def test_public_endpoints_need_no_token(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/materials").status_code == 200
    cfg = client.get("/api/config").get_json()
    assert cfg["auth_required"] is True
    assert cfg["supabase_url"] == "https://test-project.supabase.co"
    assert "service" not in json.dumps(cfg).lower()


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"<html" in r.data


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": "Basic abc"},
    {"Authorization": f"Bearer {make_token(exp_offset=-3600)}"},
    {"Authorization": f"Bearer {make_token(secret='wrong-secret-wrong-secret-wrong-secret')}"},
    {"Authorization": f"Bearer {make_token(role='anon')}"},
    {"Authorization": f"Bearer {make_token(aud='something-else')}"},
    {"Authorization": f"Bearer {make_token(iss='https://evil.example/auth/v1')}"},
])
def test_protected_routes_reject_bad_credentials(client, headers):
    for method, url in [("get", "/api/sites"), ("post", "/api/analyze/geometry"), ("get", "/api/me"),
                        ("post", "/api/uploads/sign"), ("post", "/api/notify/test")]:
        r = getattr(client, method)(url, headers=headers, json={})
        assert r.status_code == 401, (method, url, r.status_code)


def test_cors_preflight_needs_no_token(client):
    r = client.options("/api/sites", headers={"Origin": "https://app.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204
    assert "Authorization" in r.headers["Access-Control-Allow-Headers"]


def test_first_sign_in_creates_personal_org(new_user):
    u = new_user()
    me = u.get("/api/me").get_json()
    assert me["email"] == u.email and me["role"] == "owner"
    assert me["org"]["id"] != storage.default_org_id()
    # stable across requests
    assert u.get("/api/me").get_json()["org"]["id"] == me["org"]["id"]


def test_bootstrap_admin_joins_default_org(client):
    from conftest import User
    founder = User(client, "Founder@Example.com")
    me = founder.get("/api/me").get_json()
    assert me["org"]["id"] == storage.default_org_id()


# ---------------------------------------------------------------- tenant isolation -----

def test_other_org_cannot_see_or_touch_a_site(new_user):
    alice, bob = new_user(), new_user()
    site = create_site(alice)
    sid = site["id"]
    assert alice.post(f"/api/sites/{sid}/captures/geometry",
                      json={"day": 5, "capture": load("day05_capture.geojson")}).status_code == 201

    assert sid in [s["id"] for s in alice.get("/api/sites").get_json()]
    assert sid not in [s["id"] for s in bob.get("/api/sites").get_json()]

    attempts = [
        ("get", f"/api/sites/{sid}", None),
        ("get", f"/api/sites/{sid}/timeline", None),
        ("get", f"/api/sites/{sid}/alerts", None),
        ("get", f"/api/sites/{sid}/zones", None),
        ("get", f"/api/sites/{sid}/alert-rules", None),
        ("get", f"/api/sites/{sid}/report", None),
        ("get", f"/api/sites/{sid}/schedule/template", None),
        ("patch", f"/api/sites/{sid}/schedule", {"schedules": {"PAD-A": [{"pct": 100, "day": 1}]}}),
        ("patch", f"/api/sites/{sid}/targets", {"targets": {"PAD-A": 1}}),
        ("patch", f"/api/sites/{sid}/start-date", {"start_date": "2020-01-01"}),
        ("patch", f"/api/sites/{sid}/zones", {"buffers": []}),
        ("patch", f"/api/sites/{sid}/alert-rules", {"pace_threshold_pct": 99}),
        ("post", f"/api/sites/{sid}/captures/geometry", {"day": 9, "capture": load("day05_capture.geojson")}),
        ("post", f"/api/sites/{sid}/risk-check", {}),
        ("post", "/api/notify/test", {"site_id": sid}),
        ("delete", f"/api/sites/{sid}", None),
    ]
    for method, url, body in attempts:
        kw = {"json": body} if body is not None else {}
        r = getattr(bob, method)(url, **kw)
        assert r.status_code == 404, (method, url, r.status_code, r.get_data(as_text=True)[:200])

    # nothing Bob did changed Alice's site
    after = storage.get_site(sid)
    assert after is not None
    assert after["start_date"] == "2026-01-01"
    assert after["alert_rules"]["pace_threshold_pct"] == 10
    assert [c["day"] for c in storage.get_captures(sid)] == [5]
    assert alice.delete(f"/api/sites/{sid}").status_code == 204


def test_invite_joins_inviters_org(new_user):
    owner = new_user()
    site = create_site(owner)
    invited_email = f"teammate-{site['id']}@example.com"

    r = owner.post("/api/org/invites", json={"email": invited_email.upper()})
    assert r.status_code == 201
    teammate = new_user(invited_email)
    me = teammate.get("/api/me").get_json()
    assert me["org"]["id"] == owner.get("/api/me").get_json()["org"]["id"]
    assert me["role"] == "member"
    assert teammate.get(f"/api/sites/{site['id']}").status_code == 200

    members = owner.get("/api/org/members").get_json()
    assert {m["email"] for m in members["members"]} == {owner.email, invited_email}
    assert members["invites"] == []  # consumed

    # members can't invite
    assert teammate.post("/api/org/invites", json={"email": "x@example.com"}).status_code == 403
    owner.delete(f"/api/sites/{site['id']}")


def test_invite_can_be_revoked(new_user):
    owner = new_user()
    inv = owner.post("/api/org/invites", json={"email": "revoked@example.com"}).get_json()
    assert owner.delete(f"/api/org/invites/{inv['id']}").status_code == 204
    assert owner.delete(f"/api/org/invites/{inv['id']}").status_code == 404
    stranger = new_user("revoked@example.com")
    assert stranger.get("/api/me").get_json()["org"]["id"] != owner.get("/api/me").get_json()["org"]["id"]


def test_alert_rules_validation(new_user):
    u = new_user()
    sid = create_site(u)["id"]
    assert u.patch(f"/api/sites/{sid}/alert-rules", json={"slack_webhook_url": "http://169.254.169.254/"}).status_code == 400
    assert u.patch(f"/api/sites/{sid}/alert-rules", json={"email_to": ["not-an-email"]}).status_code == 400
    r = u.patch(f"/api/sites/{sid}/alert-rules", json={"email_to": "a@example.com, b@example.com"})
    assert r.status_code == 200 and r.get_json()["email_to"] == ["a@example.com", "b@example.com"]
    u.delete(f"/api/sites/{sid}")


# ---------------------------------------------------------------- file storage -----

def test_storage_paths_are_confined_to_the_callers_org():
    assert filestore.check_path("org_7/dem/abc-x.json", 7) == "org_7/dem/abc-x.json"
    for bad in ["org_8/dem/x.json", "org_70/dem/x.json", "org_7/../org_8/x.json", "/org_7/x", "org_7\\..\\x", "", None]:
        with pytest.raises(filestore.FileStoreError):
            filestore.check_path(bad, 7)


def test_upload_sign_reports_unconfigured_storage(new_user):
    r = new_user().post("/api/uploads/sign", json={"filename": "dem.json", "kind": "dem"})
    assert r.status_code == 503


def test_storage_reference_is_resolved_for_analysis(new_user, monkeypatch):
    u = new_user()
    org_id = u.get("/api/me").get_json()["org"]["id"]
    fetched = []

    def fake_download_json(path, oid):
        filestore.check_path(path, oid)
        fetched.append(path)
        return {"dem": load("blueprint.geojson"), "cap": load("day15_capture.geojson")}[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(filestore, "download_json", fake_download_json)
    r = u.post("/api/analyze/geometry", json={
        "blueprint": {"storage_path": f"org_{org_id}/blueprint/dem"},
        "capture": {"storage_path": f"org_{org_id}/capture/cap"},
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    assert round(r.get_json()["overall"], 1) == 71.4
    assert len(fetched) == 2

    # someone else's path is refused before any download happens
    r = u.post("/api/analyze/geometry", json={
        "blueprint": {"storage_path": f"org_{org_id + 1}/blueprint/dem"},
        "capture": {"storage_path": f"org_{org_id}/capture/cap"},
    })
    assert r.status_code == 404

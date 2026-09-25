"""
app.py

Backend API for the Stratum Site Progress platform.

Endpoints:
  GET  /api/health              -> {"status": "ok"}
  POST /api/analyze/geometry    -> body: {"blueprint": <geojson>, "capture": <geojson>}
  POST /api/analyze/vision      -> multipart/form-data:
                                      blueprint = <geojson as a JSON string>
                                      day       = "15"
                                      image     = <file>

Run:
  cd backend
  python -m venv venv && source venv/bin/activate   (or venv\\Scripts\\activate on Windows)
  pip install -r requirements.txt
  cp .env.example .env   # then add your ANTHROPIC_API_KEY
  python app.py

Serves on http://localhost:5001 by default.
"""

import datetime
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import sentry_setup  # noqa: E402  (must run before Flask is imported/created)

sentry_setup.init()

from flask import Flask, g, jsonify, request, Response, send_from_directory  # noqa: E402

import filestore  # noqa: E402
import storage  # noqa: E402
from auth import auth_disabled, require_auth, require_role  # noqa: E402
from geometry import analyze_geometry, get_rings  # noqa: E402
from temporal import build_timeline  # noqa: E402
from vision import analyze_vision  # noqa: E402
from reporting import generate_report_data, render_report_pdf  # noqa: E402
from schedule_import import build_template_workbook, workbook_to_bytes, import_schedule_workbook, ScheduleImportError  # noqa: E402
from volumetrics import (  # noqa: E402
    volumetric_percent_complete, remaining_earthwork, stockpile_volume,
    volume_to_tonnage, reconcile_inventory, cross_section, MATERIAL_DENSITY_PRESETS,
)
from risk import (  # noqa: E402
    detect_standing_water, check_boundary_encroachment, check_buffer_intrusion,
    check_pond_capacity, detect_erosion, evaluate_pace_rules, evaluate_risk_check_alerts,
    send_slack_alert, send_alert_email,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("stratum")

IS_PRODUCTION = bool(
    os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("APP_ENV", "").lower() == "production"
)
if auth_disabled():
    if IS_PRODUCTION:
        raise RuntimeError("AUTH_DISABLED is set on a production host. Refusing to start with authentication off.")
    log.warning("AUTH_DISABLED is set -- every request acts as the Default Organization. Local development only.")

storage.init_db()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = Flask(__name__)
# Inline JSON bodies (small DEMs/GeoJSON) still work; anything bigger should go
# through Supabase Storage (POST /api/uploads/sign) and be passed by reference.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_REQUEST_MB", "25")) * 1024 * 1024

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB, matches the frontend's own check and the vision API's per-image limit
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if "*" in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return response


@app.errorhandler(filestore.FileStoreError)
def filestore_error(e):
    return jsonify({"error": e.message}), e.status


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Request body too large. Upload big files via Storage (POST /api/uploads/sign) "
                             "and send {\"storage_path\": ...} instead of inline data."}), 413


def _site(site_id: int):
    """The caller's site, or None -- another org's site id looks exactly like a missing one."""
    return storage.get_site(site_id, g.org_id)


def _resolve(value):
    """Inline JSON passes through; {"storage_path": ...} is fetched from the caller's org folder."""
    return filestore.resolve(value, g.org_id)


def _valid_iso_date(value) -> bool:
    try:
        datetime.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


# ================================================================== frontend + public =====

@app.route("/", methods=["GET"])
def index():
    return send_from_directory(FRONTEND_DIR, "site_progress_mvp.html")


@app.route("/sample-data/<path:filename>", methods=["GET"])
def sample_data(filename):
    return send_from_directory(FRONTEND_DIR / "sample-data", filename)


@app.route("/api/config", methods=["GET"])
def public_config():
    """Public, non-secret settings the frontend needs to start Supabase Auth.
    The anon/publishable key is designed to be public; the service-role key never leaves the server."""
    return jsonify({
        "auth_required": not auth_disabled(),
        "supabase_url": os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        "storage_enabled": filestore.enabled(),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/analyze/geometry", methods=["POST", "OPTIONS"])
@require_auth
def analyze_geometry_route():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected JSON body with 'blueprint' and 'capture' keys."}), 400

    blueprint = _resolve(data.get("blueprint"))
    capture = _resolve(data.get("capture"))
    if not blueprint or not capture:
        return jsonify({"error": "Both 'blueprint' and 'capture' GeoJSON are required."}), 400

    try:
        result = analyze_geometry(blueprint, capture)
    except (KeyError, TypeError) as e:
        return jsonify({"error": f"Could not analyze this data — check that both files share matching properties.id values. ({e})"}), 400

    return jsonify(result)


@app.route("/api/analyze/vision", methods=["POST", "OPTIONS"])
@require_auth
def analyze_vision_route():
    if request.method == "OPTIONS":
        return "", 204

    blueprint_raw = request.form.get("blueprint")
    day = request.form.get("day", "?")

    if not blueprint_raw:
        return jsonify({"error": "Missing 'blueprint' form field (GeoJSON as a JSON string)."}), 400

    try:
        blueprint = _resolve(json.loads(blueprint_raw))
    except json.JSONDecodeError:
        return jsonify({"error": "'blueprint' field is not valid JSON."}), 400

    image_bytes, media_type, _path, err = _read_image_input()
    if err:
        return err

    try:
        result = analyze_vision(blueprint, image_bytes, media_type, day)
    except RuntimeError as e:
        # e.g. missing API key
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Vision analysis failed: {e}"}), 502

    return jsonify(result)


def _read_image_input():
    """The photo comes either as a multipart 'image' file, or -- for anything uploaded
    to Storage first -- as an 'image_path' form field. Returns
    (bytes, media_type, storage_path_or_None, error_response_or_None)."""
    image_file = request.files.get("image")
    image_path = request.form.get("image_path")
    if image_file:
        image_bytes, media_type = image_file.read(), (image_file.mimetype or "image/jpeg")
    elif image_path:
        image_bytes = filestore.download(image_path, g.org_id, max_bytes=MAX_IMAGE_BYTES)
        ext = image_path.rsplit(".", 1)[-1].lower()
        media_type = {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
    else:
        return None, None, None, (jsonify({"error": "Missing 'image' file (or 'image_path' from Storage)."}), 400)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return None, None, None, (jsonify({"error": "Image is over 5MB — compress it and try again."}), 400)
    if media_type not in IMAGE_TYPES:
        return None, None, None, (jsonify({"error": f"Unsupported image type '{media_type}'. Use JPEG, PNG, WebP or GIF."}), 400)
    return image_bytes, media_type, image_path, None


@app.route("/api/materials", methods=["GET"])
def materials_route():
    return jsonify(MATERIAL_DENSITY_PRESETS)


@app.route("/api/analyze/volumetric", methods=["POST", "OPTIONS"])
@require_auth
def analyze_volumetric_route():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or not all(k in data for k in ("baseline_dem", "design_dem", "asbuilt_dem")):
        return jsonify({"error": "Expected JSON body with 'baseline_dem', 'design_dem', and 'asbuilt_dem'."}), 400
    for k in ("baseline_dem", "design_dem", "asbuilt_dem"):
        data[k] = _resolve(data[k])

    try:
        vpc = volumetric_percent_complete(data["baseline_dem"], data["design_dem"], data["asbuilt_dem"])
        remaining = remaining_earthwork(data["design_dem"], data["asbuilt_dem"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (KeyError, TypeError) as e:
        return jsonify({"error": f"Malformed DEM data: {e}"}), 400

    return jsonify({**vpc, **remaining})


@app.route("/api/analyze/stockpile", methods=["POST", "OPTIONS"])
@require_auth
def analyze_stockpile_route():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or "surface_dem" not in data or "base_elevation" not in data:
        return jsonify({"error": "Expected JSON body with 'surface_dem' and 'base_elevation'."}), 400
    data["surface_dem"] = _resolve(data["surface_dem"])

    material = data.get("material", "crushed_aggregate")
    density = data.get("custom_density_t_per_m3") or MATERIAL_DENSITY_PRESETS.get(material)
    if density is None:
        return jsonify({"error": f"Unknown material '{material}'. Valid presets: {list(MATERIAL_DENSITY_PRESETS.keys())}, or pass 'custom_density_t_per_m3'."}), 400

    try:
        volume = stockpile_volume(data["surface_dem"], data["base_elevation"], footprint_ring=data.get("footprint"))
    except (KeyError, TypeError) as e:
        return jsonify({"error": f"Malformed DEM/footprint data: {e}"}), 400

    tonnage = volume_to_tonnage(volume, density)
    response = {"volume_m3": volume, "material": material, "density_t_per_m3": density, "tonnage": tonnage}

    if "recorded_tons" in data and data["recorded_tons"] is not None:
        response["reconciliation"] = reconcile_inventory(
            tonnage, float(data["recorded_tons"]), tolerance_pct=data.get("tolerance_pct", 5.0)
        )

    return jsonify(response)


@app.route("/api/analyze/cross-section", methods=["POST", "OPTIONS"])
@require_auth
def cross_section_route():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or not data.get("dems") or not data.get("point_a") or not data.get("point_b"):
        return jsonify({"error": "Expected JSON body with 'dems' (object of named DEMs), 'point_a', and 'point_b' ([x,y] each)."}), 400
    if not isinstance(data["dems"], dict):
        return jsonify({"error": "'dems' must be an object of {label: dem}."}), 400
    data["dems"] = {label: _resolve(dem) for label, dem in data["dems"].items()}

    try:
        samples = cross_section(data["dems"], data["point_a"], data["point_b"], num_samples=data.get("num_samples", 50))
    except (KeyError, TypeError) as e:
        return jsonify({"error": f"Malformed DEM data: {e}"}), 400

    return jsonify({"samples": samples})


@app.route("/api/sites", methods=["GET", "POST", "OPTIONS"])
@require_auth
def sites_route():
    if request.method == "OPTIONS":
        return "", 204

    if request.method == "GET":
        return jsonify(storage.list_sites(g.org_id))

    data = request.get_json(silent=True)
    if not data or not data.get("name") or not data.get("blueprint"):
        return jsonify({"error": "Expected JSON body with 'name' and 'blueprint'."}), 400
    blueprint = _resolve(data["blueprint"])
    if not isinstance(blueprint, dict) or not isinstance(blueprint.get("features"), list):
        return jsonify({"error": "'blueprint' must be a GeoJSON FeatureCollection."}), 400

    start_date = data.get("start_date")
    if start_date and not _valid_iso_date(start_date):
        return jsonify({"error": f"'start_date' must be an ISO date (YYYY-MM-DD), got '{start_date}'."}), 400

    site_id = storage.create_site(data["name"], blueprint, start_date, org_id=g.org_id)
    return jsonify(_site(site_id)), 201


@app.route("/api/sites/<int:site_id>/start-date", methods=["PATCH", "OPTIONS"])
@require_auth
def start_date_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or not data.get("start_date"):
        return jsonify({"error": "Expected JSON body with 'start_date' (YYYY-MM-DD)."}), 400

    if not _valid_iso_date(data["start_date"]):
        return jsonify({"error": f"'start_date' must be an ISO date (YYYY-MM-DD), got '{data['start_date']}'."}), 400

    site = storage.update_start_date(site_id, data["start_date"], g.org_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404
    return jsonify(site)


@app.route("/api/sites/<int:site_id>", methods=["GET", "DELETE", "OPTIONS"])
@require_auth
def site_detail_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    if request.method == "DELETE":
        storage.delete_site(site_id, g.org_id)
        return "", 204

    return jsonify(site)


@app.route("/api/sites/<int:site_id>/captures/geometry", methods=["POST", "OPTIONS"])
@require_auth
def add_geometry_capture_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    data = request.get_json(silent=True)
    if not data or "day" not in data or not data.get("capture"):
        return jsonify({"error": "Expected JSON body with 'day' (integer) and 'capture' (GeoJSON)."}), 400

    try:
        day = int(data["day"])
    except (TypeError, ValueError):
        return jsonify({"error": "'day' must be an integer."}), 400

    try:
        result = analyze_geometry(site["blueprint"], _resolve(data["capture"]))
    except (KeyError, TypeError) as e:
        return jsonify({"error": f"Could not analyze this data — check properties.id values match the blueprint. ({e})"}), 400

    storage.add_capture(site_id, day, "geometry", result)
    return jsonify(build_timeline(site, storage.get_captures(site_id))), 201


@app.route("/api/sites/<int:site_id>/captures/vision", methods=["POST", "OPTIONS"])
@require_auth
def add_vision_capture_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    day = request.form.get("day")
    if day is None:
        return jsonify({"error": "Expected form fields 'day' and 'image' (or 'image_path')."}), 400

    try:
        day = int(day)
    except ValueError:
        return jsonify({"error": "'day' must be an integer."}), 400

    image_bytes, media_type, image_path, err = _read_image_input()
    if err:
        return err

    try:
        result = analyze_vision(site["blueprint"], image_bytes, media_type, str(day))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Vision analysis failed: {e}"}), 502

    # Keep the source photo so a vision score can be audited later, not just trusted.
    if not image_path and filestore.enabled():
        try:
            image_path = filestore.upload_bytes(g.org_id, "photo", f"site{site_id}_day{day}.jpg", image_bytes, media_type)
        except filestore.FileStoreError as e:
            log.warning("could not archive vision photo: %s", e.message)
    if image_path:
        result["image_path"] = image_path
    storage.add_capture(site_id, day, "vision", result)
    return jsonify(build_timeline(site, storage.get_captures(site_id))), 201


@app.route("/api/sites/<int:site_id>/schedule", methods=["PATCH", "OPTIONS"])
@require_auth
def update_schedule_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or "schedules" not in data or not isinstance(data["schedules"], dict):
        return jsonify({"error": "Expected JSON body with 'schedules': {feature_id: [{\"pct\":10,\"day\":5}, ...] or null, ...}."}), 400

    for fid, milestones in data["schedules"].items():
        if milestones is None:
            continue
        if not isinstance(milestones, list):
            return jsonify({"error": f"Schedule for '{fid}' must be a list of {{'pct','day'}} objects or null."}), 400
        for m in milestones:
            if not isinstance(m, dict) or "pct" not in m or "day" not in m:
                return jsonify({"error": f"Each milestone for '{fid}' needs both 'pct' and 'day'."}), 400

    site = storage.update_schedules(site_id, data["schedules"], g.org_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    return jsonify(build_timeline(site, storage.get_captures(site_id)))


@app.route("/api/sites/<int:site_id>/schedule/template", methods=["GET"])
@require_auth
def schedule_template_route(site_id):
    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    wb = build_template_workbook(site)
    xlsx_bytes = workbook_to_bytes(wb)
    filename = f"{site['name'].replace(' ', '_')}_schedule_template.xlsx"
    return Response(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/sites/<int:site_id>/schedule/import-excel", methods=["POST", "OPTIONS"])
@require_auth
def schedule_import_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Expected a multipart 'file' field with the completed .xlsx."}), 400

    try:
        result = import_schedule_workbook(file.read(), site["blueprint"]["features"], site.get("start_date"))
    except ScheduleImportError as e:
        return jsonify({"error": str(e)}), 400

    updated_site = storage.update_schedules(site_id, result["schedules"], g.org_id)
    timeline = build_timeline(updated_site, storage.get_captures(site_id))
    return jsonify({
        "imported_feature_count": len(result["schedules"]),
        "warnings": result["warnings"],
        "timeline": timeline,
    })


@app.route("/api/sites/<int:site_id>/targets", methods=["PATCH", "OPTIONS"])
@require_auth
def update_targets_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or "targets" not in data or not isinstance(data["targets"], dict):
        return jsonify({"error": "Expected JSON body with 'targets': {feature_id: target_day_or_null, ...}."}), 400

    site = storage.update_target_days(site_id, data["targets"], g.org_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    return jsonify(build_timeline(site, storage.get_captures(site_id)))


@app.route("/api/sites/<int:site_id>/timeline", methods=["GET"])
@require_auth
def timeline_route(site_id):
    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404
    return jsonify(build_timeline(site, storage.get_captures(site_id)))


@app.route("/api/sites/<int:site_id>/zones", methods=["GET", "PATCH", "OPTIONS"])
@require_auth
def zones_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    if request.method == "GET":
        return jsonify(site["reference_zones"])

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected a JSON body with any of: boundary, buffers, restricted_zones, pond_configs."}), 400

    updated = storage.update_reference_zones(site_id, data, g.org_id)
    return jsonify(updated["reference_zones"])


@app.route("/api/sites/<int:site_id>/alert-rules", methods=["GET", "PATCH", "OPTIONS"])
@require_auth
def alert_rules_route(site_id):
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    if request.method == "GET":
        return jsonify(site["alert_rules"])

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected a JSON body with any of: pace_threshold_pct, notify_slack, notify_email, "
                                 "email_to, slack_webhook_url."}), 400

    if "email_to" in data:
        emails = data["email_to"]
        if isinstance(emails, str):
            emails = [e.strip() for e in emails.split(",") if e.strip()]
        if not isinstance(emails, list) or not all(isinstance(e, str) and "@" in e for e in emails) or len(emails) > 50:
            return jsonify({"error": "'email_to' must be a list (or comma-separated string) of up to 50 email addresses."}), 400
        data["email_to"] = emails
    if data.get("slack_webhook_url") and not str(data["slack_webhook_url"]).startswith("https://hooks.slack.com/"):
        return jsonify({"error": "'slack_webhook_url' must be a Slack incoming-webhook URL (https://hooks.slack.com/...)."}), 400

    updated = storage.update_alert_rules(site_id, data, g.org_id)
    return jsonify(updated["alert_rules"])


@app.route("/api/sites/<int:site_id>/alerts", methods=["GET"])
@require_auth
def alerts_log_route(site_id):
    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404
    return jsonify(storage.get_alerts(site_id))


@app.route("/api/sites/<int:site_id>/report", methods=["GET"])
@require_auth
def report_route(site_id):
    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    try:
        period_days = int(request.args.get("period_days", 7))
    except ValueError:
        return jsonify({"error": "'period_days' must be an integer."}), 400

    fmt = request.args.get("format", "json")
    if fmt not in ("json", "pdf"):
        return jsonify({"error": "'format' must be 'json' or 'pdf'."}), 400

    captures = storage.get_captures(site_id)
    timeline = build_timeline(site, captures)
    alerts = storage.get_alerts(site_id)
    report = generate_report_data(site, timeline, alerts, period_days=period_days)

    if fmt == "json":
        return jsonify(report)

    pdf_bytes = render_report_pdf(report)
    filename = f"{site['name'].replace(' ', '_')}_report.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/sites/<int:site_id>/risk-check", methods=["POST", "OPTIONS"])
@require_auth
def risk_check_route(site_id):
    """
    Runs whichever risk/compliance checks the supplied data supports, against
    the site's saved reference zones, persists any newly-triggered alerts,
    and optionally sends them via Slack/email if the site's alert rules ask
    for it and .env has the corresponding webhook/SMTP settings.

    Body (all optional -- only the checks with matching data actually run):
      capture               : GeoJSON as-built features -> boundary + buffer checks
      asbuilt_dem            : elevation grid -> standing water + pond capacity
      prev_dem                : elevation grid from an earlier survey -> erosion (paired with asbuilt_dem)
      water_threshold_elev   : float, required if asbuilt_dem given and standing-water check wanted
      erosion_drop_threshold_m: float, default 0.5
      run_pace_check          : bool, default true -- uses the site's stored capture history + schedule
      send_notifications      : bool, default false
    """
    if request.method == "OPTIONS":
        return "", 204

    site = _site(site_id)
    if not site:
        return jsonify({"error": "Site not found."}), 404

    data = request.get_json(silent=True) or {}
    for k in ("capture", "asbuilt_dem", "prev_dem"):
        if data.get(k):
            data[k] = _resolve(data[k])
    zones = site["reference_zones"]
    rules = site["alert_rules"]
    risk_results = []

    try:
        # ---- boundary + buffer checks (need an as-built capture) ----
        if data.get("capture"):
            features = data["capture"].get("features", [])
            if zones.get("boundary"):
                risk_results.append(check_boundary_encroachment(features, zones["boundary"]["ring"]))
            if zones.get("buffers"):
                risk_results.append(check_buffer_intrusion(features, zones["buffers"]))

        # ---- standing water + pond capacity (need a DEM) ----
        if data.get("asbuilt_dem"):
            asbuilt_dem = data["asbuilt_dem"]
            pond_rings = [get_rings(f["geometry"])[0] for f in site["blueprint"].get("features", [])
                          if f["properties"].get("category") == "Retention Pond" and get_rings(f["geometry"])]

            if "water_threshold_elev" in data:
                risk_results.append(detect_standing_water(asbuilt_dem, float(data["water_threshold_elev"]), pond_rings))

            for pond_id, cfg in (zones.get("pond_configs") or {}).items():
                pond_feature = next((f for f in site["blueprint"].get("features", []) if f["properties"]["id"] == pond_id), None)
                if not pond_feature:
                    continue
                rings = get_rings(pond_feature["geometry"])
                if not rings:
                    continue
                result = check_pond_capacity(asbuilt_dem, rings[0], float(cfg["rim_elevation"]), float(cfg["min_capacity_m3"]))
                result["pond_id"] = pond_id
                risk_results.append(result)

            # ---- erosion (needs a paired earlier survey) ----
            if data.get("prev_dem"):
                active_rings = [get_rings(f["geometry"])[0] for f in site["blueprint"].get("features", []) if get_rings(f["geometry"])]
                threshold = float(data.get("erosion_drop_threshold_m", 0.5))
                risk_results.append(detect_erosion(data["prev_dem"], asbuilt_dem, active_rings, threshold))

    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"Malformed input for risk checks: {e}"}), 400

    # ---- alerts from risk checks ----
    alerts = evaluate_risk_check_alerts(risk_results)

    # ---- pace alerts from the site's own capture history ----
    if data.get("run_pace_check", True):
        captures = storage.get_captures(site_id)
        if captures:
            timeline = build_timeline(site, captures)
            alerts.extend(evaluate_pace_rules(timeline["features"], rules.get("pace_threshold_pct", 10)))

    notification_results = {}
    if alerts and data.get("send_notifications"):
        slack_url = rules.get("slack_webhook_url") or os.environ.get("SLACK_WEBHOOK_URL")
        if rules.get("notify_slack") and slack_url:
            notification_results["slack"] = send_slack_alert(slack_url, alerts, site["name"])
        if rules.get("notify_email"):
            notification_results["email"] = send_alert_email(alerts, site["name"], _alert_recipients(rules))

    if alerts:
        for a in alerts:
            a["slack_sent"] = notification_results.get("slack", {}).get("sent", False)
            a["email_sent"] = notification_results.get("email", {}).get("sent", False)
        storage.add_alerts(site_id, alerts)

    return jsonify({"risk_results": risk_results, "alerts": alerts, "notifications": notification_results})


@app.route("/api/notify/test", methods=["POST", "OPTIONS"])
@require_auth
def notify_test_route():
    """Sends a single real test alert via Slack and/or email, using whatever's
    configured in .env. Reports honest success/failure -- never fakes a result."""
    if request.method == "OPTIONS":
        return "", 204

    test_alert = [{"rule_type": "test", "severity": "medium", "feature_id": None,
                   "message": "This is a test alert from Stratum's notification settings."}]
    results = {}

    data = request.get_json(silent=True) or {}
    rules = {}
    if data.get("site_id") is not None:
        try:
            site = _site(int(data["site_id"]))
        except (TypeError, ValueError):
            site = None
        if not site:
            return jsonify({"error": "Site not found."}), 404
        rules = site["alert_rules"]

    slack_url = rules.get("slack_webhook_url") or os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        results["slack"] = send_slack_alert(slack_url, test_alert, "Notification Test")
    else:
        results["slack"] = {"sent": False, "reason": "No Slack webhook configured (site alert rules or SLACK_WEBHOOK_URL)."}

    # A test goes to the person who clicked the button (or the site's configured list).
    recipients = rules.get("email_to") or ([g.email] if g.get("email") else _alert_recipients(rules))
    results["email"] = send_alert_email(test_alert, "Notification Test", recipients)

    return jsonify(results)


def _alert_recipients(rules: dict) -> list:
    """Per-site list if set, otherwise everyone in the site's organization. The global
    ALERT_TO_EMAIL is only a fallback in single-tenant (AUTH_DISABLED) mode, so one
    customer's alerts can never be mailed to another customer's address."""
    if rules.get("email_to"):
        return list(rules["email_to"])
    emails = storage.org_member_emails(g.org_id) if g.get("org_id") else []
    if not emails and auth_disabled() and os.environ.get("ALERT_TO_EMAIL"):
        emails = [e.strip() for e in os.environ["ALERT_TO_EMAIL"].split(",") if e.strip()]
    return emails


# ======================================================================= account / org =====

@app.route("/api/me", methods=["GET"])
@require_auth
def me_route():
    org = storage.get_org(g.org_id) if g.org_id else None
    return jsonify({"user_id": g.user_id, "email": g.email, "role": g.role, "org": org})


@app.route("/api/org/members", methods=["GET"])
@require_auth
def org_members_route():
    return jsonify({"members": storage.list_org_members(g.org_id), "invites": storage.list_org_invites(g.org_id)})


@app.route("/api/org/invites", methods=["POST", "OPTIONS"])
@require_auth
@require_role("owner", "admin")
def org_invite_route():
    """Invite a teammate by email. They join this organization the first time they
    sign in with that address (instead of getting their own empty org)."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    role = data.get("role", "member")
    if "@" not in email:
        return jsonify({"error": "Expected JSON body with a valid 'email'."}), 400
    if role not in ("member", "admin") and not (role == "owner" and g.role == "owner"):
        return jsonify({"error": "'role' must be 'member' or 'admin'."}), 400
    return jsonify(storage.add_org_invite(g.org_id, email, role)), 201


@app.route("/api/org/invites/<int:invite_id>", methods=["DELETE", "OPTIONS"])
@require_auth
@require_role("owner", "admin")
def org_invite_delete_route(invite_id):
    if not storage.delete_org_invite(g.org_id, invite_id):
        return jsonify({"error": "Invite not found."}), 404
    return "", 204


# ============================================================================= uploads =====

@app.route("/api/uploads/sign", methods=["POST", "OPTIONS"])
@require_auth
def upload_sign_route():
    """Step 1 of a large upload: returns a short-lived URL the browser PUTs the raw file
    to (straight to Supabase Storage, never through this server), plus the
    storage_path to pass to any analysis endpoint as {"storage_path": ...}."""
    data = request.get_json(silent=True) or {}
    if not data.get("filename"):
        return jsonify({"error": "Expected JSON body with 'filename' and 'kind' (dem, capture, blueprint, photo, pointcloud, other)."}), 400
    return jsonify(filestore.create_signed_upload(g.org_id, data.get("kind", "other"), data["filename"])), 201


@app.route("/api/files/url", methods=["GET"])
@require_auth
def file_url_route():
    """Short-lived download link for one of the caller's own stored files (e.g. the photo
    behind a vision capture)."""
    path = request.args.get("path", "")
    return jsonify({"url": filestore.signed_download_url(path, g.org_id)})


if __name__ == "__main__":
    # Local development only. In production gunicorn serves `app:app` (see Procfile / render.yaml).
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "1") == "1")

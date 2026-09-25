"""
risk.py

Tier 3: Risk, Compliance & Anomaly Detection.

All checks here operate on data we already have -- elevation grids (DEMs)
and GeoJSON polygons -- using the same verified math from geometry.py
(point-in-polygon, polygon clipping) and volumetrics.py (grid sampling).
Nothing in this file is a label or a guess; every flag is backed by an
actual number (an area, a volume, an elevation delta) that a human could
recompute by hand from the same inputs.
"""

import smtplib
from email.mime.text import MIMEText

import requests

from geometry import clip_polygon, polygon_area, get_rings
from volumetrics import cell_area, point_in_polygon


# ============================================================= Standing water =====

def _cluster_flagged_cells(flagged: list, dem: dict, min_cluster_cells: int = 2) -> list:
    """
    4-connectivity flood fill over a set of (row, col) flagged cells.
    Returns a list of clusters, each with area, cell count, centroid (world
    coords), and bounding box -- discarding clusters smaller than
    min_cluster_cells (single stray cells are usually noise, not a real patch).
    """
    flagged_set = set(flagged)
    visited = set()
    clusters = []
    cs = dem["cell_size"]

    for start in flagged:
        if start in visited:
            continue
        stack = [start]
        component = []
        visited.add(start)
        while stack:
            r, c = stack.pop()
            component.append((r, c))
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (r + dr, c + dc)
                if nb in flagged_set and nb not in visited:
                    visited.add(nb)
                    stack.append(nb)

        if len(component) < min_cluster_cells:
            continue

        rows = [p[0] for p in component]
        cols = [p[1] for p in component]
        xs = [dem["origin_x"] + c * cs for c in cols]
        ys = [dem["origin_y"] + r * cs for r in rows]
        clusters.append({
            "cell_count": len(component),
            "area_m2": len(component) * (cs ** 2),
            "centroid": [sum(xs) / len(xs) + cs / 2, sum(ys) / len(ys) + cs / 2],
            "bbox": [min(xs), min(ys), max(xs) + cs, max(ys) + cs],
        })

    clusters.sort(key=lambda c: -c["area_m2"])
    return clusters


def detect_standing_water(asbuilt_dem: dict, water_threshold_elev: float, pond_rings: list) -> dict:
    """
    Flags grid cells at or below `water_threshold_elev` that fall OUTSIDE
    every polygon in `pond_rings` (the site's designated retention areas).
    Water pooling inside a pond is expected; water pooling anywhere else on
    a graded site usually means poor drainage, a low spot, or a compaction
    problem worth a look.
    """
    cs = asbuilt_dem["cell_size"]
    flagged = []
    for r, row in enumerate(asbuilt_dem["values"]):
        y = asbuilt_dem["origin_y"] + r * cs
        for c, z in enumerate(row):
            if z > water_threshold_elev:
                continue
            x = asbuilt_dem["origin_x"] + c * cs
            in_pond = any(point_in_polygon(x, y, ring) for ring in pond_rings)
            if not in_pond:
                flagged.append((r, c))

    clusters = _cluster_flagged_cells(flagged, asbuilt_dem)
    return {
        "check": "standing_water",
        "threshold_elev": water_threshold_elev,
        "flagged_cell_count": len(flagged),
        "total_flagged_area_m2": len(flagged) * (cs ** 2),
        "clusters": clusters,
        "triggered": len(clusters) > 0,
    }


# ============================================================= Boundary / buffer =====

def _feature_area_outside(feature_ring: list, allowed_ring: list) -> float:
    """Area of feature_ring that falls OUTSIDE allowed_ring (i.e. total - inside)."""
    total = polygon_area(feature_ring)
    inside = clip_polygon(feature_ring, allowed_ring)
    inside_area = polygon_area(inside) if len(inside) >= 3 else 0.0
    return max(0.0, total - inside_area)


def _feature_area_inside(feature_ring: list, forbidden_ring: list) -> float:
    """Area of feature_ring that falls INSIDE forbidden_ring (any overlap at all)."""
    inside = clip_polygon(feature_ring, forbidden_ring)
    return polygon_area(inside) if len(inside) >= 3 else 0.0


def check_boundary_encroachment(features: list, boundary_ring: list) -> dict:
    """Flags any as-built feature that extends outside the permit/property boundary."""
    flags = []
    for f in features:
        fid = f["properties"]["id"]
        name = f["properties"].get("name", fid)
        for ring in get_rings(f.get("geometry")):
            outside_area = _feature_area_outside(ring, boundary_ring)
            if outside_area > 1e-6:
                flags.append({"feature_id": fid, "name": name, "encroachment_area_m2": outside_area})
    return {"check": "boundary_encroachment", "flags": flags, "triggered": len(flags) > 0}


def check_buffer_intrusion(features: list, buffer_zones: list) -> dict:
    """
    Flags any as-built feature that overlaps a protected buffer/setback zone
    (stream buffers, wetland setbacks, vegetation clearance limits -- any
    area that's supposed to stay undisturbed). `buffer_zones` is a list of
    {"id","name","ring"}.
    """
    flags = []
    for f in features:
        fid = f["properties"]["id"]
        name = f["properties"].get("name", fid)
        for ring in get_rings(f.get("geometry")):
            for bz in buffer_zones:
                intrusion_area = _feature_area_inside(ring, bz["ring"])
                if intrusion_area > 1e-6:
                    flags.append({
                        "feature_id": fid, "name": name,
                        "buffer_id": bz["id"], "buffer_name": bz["name"],
                        "intrusion_area_m2": intrusion_area,
                    })
    return {"check": "buffer_intrusion", "flags": flags, "triggered": len(flags) > 0}


# ============================================================= Pond capacity =====

def check_pond_capacity(asbuilt_dem: dict, pond_ring: list, rim_elevation: float, min_capacity_m3: float) -> dict:
    """
    Available storage capacity = volume between the current surface and the
    rim (spillway) elevation, integrated over the pond footprint only.
    Flags if that's below the minimum required for the design storm event.
    """
    cs = asbuilt_dem["cell_size"]
    area = cell_area(asbuilt_dem)
    capacity = 0.0
    for r, row in enumerate(asbuilt_dem["values"]):
        y = asbuilt_dem["origin_y"] + r * cs
        for c, z in enumerate(row):
            x = asbuilt_dem["origin_x"] + c * cs
            if not point_in_polygon(x, y, pond_ring):
                continue
            if z < rim_elevation:
                capacity += (rim_elevation - z) * area

    return {
        "check": "pond_capacity",
        "available_capacity_m3": capacity,
        "min_required_m3": min_capacity_m3,
        "deficit_m3": max(0.0, min_capacity_m3 - capacity),
        "triggered": capacity < min_capacity_m3,
    }


# ============================================================= Erosion =====

def detect_erosion(prev_dem: dict, curr_dem: dict, active_work_rings: list, drop_threshold_m: float) -> dict:
    """
    Compares two elevation surveys. Flags cells OUTSIDE every active work
    zone (areas that should be stable) where the surface dropped by more
    than `drop_threshold_m` -- material loss with no corresponding planned
    work is the signature of erosion, not grading.
    """
    if (prev_dem["cell_size"] != curr_dem["cell_size"] or prev_dem["nrows"] != curr_dem["nrows"]
            or prev_dem["ncols"] != curr_dem["ncols"]):
        raise ValueError("prev_dem and curr_dem must share the same grid definition")

    cs = curr_dem["cell_size"]
    flagged = []
    for r in range(curr_dem["nrows"]):
        y = curr_dem["origin_y"] + r * cs
        for c in range(curr_dem["ncols"]):
            x = curr_dem["origin_x"] + c * cs
            drop = prev_dem["values"][r][c] - curr_dem["values"][r][c]
            if drop < drop_threshold_m:
                continue
            if any(point_in_polygon(x, y, ring) for ring in active_work_rings):
                continue  # expected change inside an active work zone
            flagged.append((r, c))

    clusters = _cluster_flagged_cells(flagged, curr_dem)
    return {
        "check": "erosion",
        "drop_threshold_m": drop_threshold_m,
        "flagged_cell_count": len(flagged),
        "clusters": clusters,
        "triggered": len(clusters) > 0,
    }


# ============================================================= Alert rules engine =====

def evaluate_pace_rules(features: list, threshold_pct: float) -> list:
    """
    features: the temporal engine's per-feature series (from temporal.py),
    each with 'variance_pct'. Flags any feature more than threshold_pct
    percentage points behind its planned pace.
    """
    alerts = []
    for f in features:
        vp = f.get("variance_pct")
        if vp is not None and vp < -abs(threshold_pct):
            alerts.append({
                "rule_type": "behind_pace", "feature_id": f["id"], "feature_name": f["name"],
                "message": f"{f['name']} is {abs(vp):.1f} points behind planned pace (threshold {threshold_pct}%).",
                "severity": "high" if abs(vp) > threshold_pct * 2 else "medium",
            })
    return alerts


def evaluate_risk_check_alerts(risk_results: list) -> list:
    """Turns triggered risk/compliance checks into the same alert shape as pace rules."""
    alerts = []
    for r in risk_results:
        if not r.get("triggered"):
            continue
        if r["check"] == "standing_water":
            alerts.append({"rule_type": "standing_water", "feature_id": None,
                            "message": f"Standing water detected outside pond zones: {len(r['clusters'])} patch(es), {r['total_flagged_area_m2']:.0f} m2 total.",
                            "severity": "medium"})
        elif r["check"] == "boundary_encroachment":
            for flag in r["flags"]:
                alerts.append({"rule_type": "boundary_encroachment", "feature_id": flag["feature_id"],
                                "message": f"{flag['name']} extends {flag['encroachment_area_m2']:.1f} m2 outside the permit boundary.",
                                "severity": "high"})
        elif r["check"] == "buffer_intrusion":
            for flag in r["flags"]:
                alerts.append({"rule_type": "buffer_intrusion", "feature_id": flag["feature_id"],
                                "message": f"{flag['name']} intrudes {flag['intrusion_area_m2']:.1f} m2 into buffer zone '{flag['buffer_name']}'.",
                                "severity": "high"})
        elif r["check"] == "pond_capacity":
            alerts.append({"rule_type": "pond_capacity", "feature_id": None,
                            "message": f"Pond available capacity is {r['available_capacity_m3']:.0f} m3, {r['deficit_m3']:.0f} m3 below the {r['min_required_m3']:.0f} m3 minimum.",
                            "severity": "high"})
        elif r["check"] == "erosion":
            alerts.append({"rule_type": "erosion", "feature_id": None,
                            "message": f"Possible erosion detected outside active work zones: {len(r['clusters'])} patch(es).",
                            "severity": "medium"})
    return alerts


# ============================================================= Notification delivery =====

def send_slack_alert(webhook_url: str, alerts: list, site_name: str) -> dict:
    """Real Slack incoming-webhook POST. Requires a genuine webhook URL from the
    user's own Slack workspace -- this code is correct but its actual delivery
    can't be verified from an environment with no outbound network access."""
    if not alerts:
        return {"sent": False, "reason": "no alerts to send"}
    lines = [f"*Stratum alert -- {site_name}*"] + [f"- [{a['severity'].upper()}] {a['message']}" for a in alerts]
    payload = {"text": "\n".join(lines)}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return {"sent": True}
    except requests.RequestException as e:
        return {"sent": False, "reason": str(e)}


def send_email_alert(smtp_host: str, smtp_port: int, username: str, password: str,
                      from_addr: str, to_addr: str, alerts: list, site_name: str) -> dict:
    """Real SMTP send. Same caveat as send_slack_alert -- correct code, unverifiable
    delivery in a no-network sandbox."""
    if not alerts:
        return {"sent": False, "reason": "no alerts to send"}
    lines = [f"Stratum alert -- {site_name}", ""] + [f"[{a['severity'].upper()}] {a['message']}" for a in alerts]
    msg = MIMEText("\n".join(lines))
    msg["Subject"] = f"Stratum alert: {site_name} ({len(alerts)} item{'s' if len(alerts) != 1 else ''})"
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            if username:
                server.login(username, password)
            server.sendmail(from_addr, [a.strip() for a in to_addr.split(",") if a.strip()], msg.as_string())
        return {"sent": True}
    except Exception as e:
        return {"sent": False, "reason": str(e)}


def _alert_email_content(alerts: list, site_name: str) -> tuple[str, str]:
    subject = f"Stratum alert: {site_name} ({len(alerts)} item{'s' if len(alerts) != 1 else ''})"
    lines = [f"Stratum alert -- {site_name}", ""] + [f"[{a['severity'].upper()}] {a['message']}" for a in alerts]
    return subject, "\n".join(lines)


def send_resend_email(api_key: str, from_addr: str, to_addrs: list, alerts: list, site_name: str) -> dict:
    """Sends via Resend's HTTP API (https://resend.com/docs/api-reference/emails/send-email).
    Preferred over raw SMTP: authenticated sending domain, far better deliverability,
    and a plain HTTPS call instead of an SMTP session. `from_addr` must be on a domain
    verified in Resend (or onboarding@resend.dev while testing, which can only deliver
    to your own Resend account address)."""
    if not alerts:
        return {"sent": False, "reason": "no alerts to send"}
    if not to_addrs:
        return {"sent": False, "reason": "no recipients configured"}
    subject, text = _alert_email_content(alerts, site_name)
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": list(to_addrs)[:50], "subject": subject, "text": text},
            timeout=10,
        )
        if resp.status_code >= 400:
            return {"sent": False, "reason": f"Resend {resp.status_code}: {resp.text[:300]}"}
        return {"sent": True, "provider": "resend", "id": resp.json().get("id")}
    except requests.RequestException as e:
        return {"sent": False, "reason": str(e)}


def send_alert_email(alerts: list, site_name: str, to_addrs: list, env=None) -> dict:
    """Picks the configured email provider: Resend if RESEND_API_KEY is set, otherwise
    SMTP if SMTP_HOST is set. Returns {"sent": False, "reason": ...} if neither is."""
    import os
    env = env if env is not None else os.environ
    from_addr = env.get("ALERT_FROM_EMAIL", "")
    if env.get("RESEND_API_KEY"):
        if not from_addr:
            return {"sent": False, "reason": "ALERT_FROM_EMAIL is not set"}
        return send_resend_email(env["RESEND_API_KEY"], from_addr, to_addrs, alerts, site_name)
    if env.get("SMTP_HOST"):
        if not to_addrs:
            return {"sent": False, "reason": "no recipients configured"}
        return send_email_alert(
            env["SMTP_HOST"], int(env.get("SMTP_PORT", 587)), env.get("SMTP_USERNAME", ""),
            env.get("SMTP_PASSWORD", ""), from_addr, ", ".join(to_addrs), alerts, site_name,
        )
    return {"sent": False, "reason": "No email provider configured (set RESEND_API_KEY, or SMTP_HOST)."}

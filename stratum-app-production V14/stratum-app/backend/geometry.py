"""
geometry.py

Pure-Python implementation of the "Measured" analysis mode: compares a
planned blueprint (GeoJSON FeatureCollection of Polygon/MultiPolygon
features) against an as-built site capture in the same schema, and computes
an area-weighted completion score.

Matched features (by properties.id) are clipped against the planned polygon
(Sutherland-Hodgman) so overbuilding can't push a feature above 100%.
"""

from typing import Optional


def polygon_area(ring: list[list[float]]) -> float:
    """
    Shoelace formula. `ring` is a list of [x, y] points.

    Uses modulo wraparound indexing rather than assuming the ring is
    explicitly closed (first point repeated at the end). This matters
    because clip_polygon() returns OPEN rings (no repeated closing point)
    whenever it actually clips something -- as opposed to a pass-through
    case where the input happens to already be fully inside the clip
    boundary, which preserves whatever closure the input had. Assuming
    closure unconditionally silently dropped the final edge for any
    genuinely-clipped, non-corner-anchored polygon, producing wrong areas
    that went undetected until a test case forced real multi-edge clipping.
    """
    n = len(ring)
    area = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area / 2.0)


def get_rings(geometry: Optional[dict]) -> list[list[list[float]]]:
    """Return outer rings only (holes are ignored, matching the frontend)."""
    if not geometry:
        return []
    gtype = geometry.get("type")
    if gtype == "Polygon":
        return [geometry["coordinates"][0]]
    if gtype == "MultiPolygon":
        return [poly[0] for poly in geometry["coordinates"]]
    return []


def feature_area(feature: dict) -> float:
    rings = get_rings(feature.get("geometry"))
    return sum(polygon_area(r) for r in rings)


def _inside(p, a, b) -> bool:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0


def _intersect(p1, p2, a, b):
    a1 = p2[1] - p1[1]
    b1 = p1[0] - p2[0]
    c1 = a1 * p1[0] + b1 * p1[1]
    a2 = b[1] - a[1]
    b2 = a[0] - b[0]
    c2 = a2 * a[0] + b2 * a[1]
    det = a1 * b2 - a2 * b1
    if abs(det) < 1e-9:
        return p2
    return [(b2 * c1 - b1 * c2) / det, (a1 * c2 - a2 * c1) / det]


def clip_polygon(subject: list, clip: list) -> list:
    """Sutherland-Hodgman: clip `subject` ring against convex `clip` ring."""
    output = list(subject)
    for i in range(len(clip) - 1):
        if not output:
            break
        a, b = clip[i], clip[i + 1]
        input_list = output
        output = []
        for j in range(len(input_list)):
            current = input_list[j]
            prev = input_list[j - 1]
            cur_in = _inside(current, a, b)
            prev_in = _inside(prev, a, b)
            if cur_in:
                if not prev_in:
                    output.append(_intersect(prev, current, a, b))
                output.append(current)
            elif prev_in:
                output.append(_intersect(prev, current, a, b))
    return output


def overlap_area(planned_ring: list, built_ring: list) -> float:
    clipped = clip_polygon(built_ring, planned_ring)
    if len(clipped) < 3:
        return 0.0
    return polygon_area(clipped)


def status_from_pct(pct: float) -> str:
    if pct >= 95:
        return "complete"
    if pct > 0:
        return "progress"
    return "notstarted"


def analyze_geometry(blueprint: dict, capture: dict) -> dict:
    """
    Compare planned vs as-built GeoJSON FeatureCollections.
    Returns the same shape the frontend's render() function expects.
    """
    by_id = {f["properties"]["id"]: f for f in capture.get("features", [])}

    rows = []
    for bf in blueprint.get("features", []):
        fid = bf["properties"]["id"]
        planned_rings = get_rings(bf.get("geometry"))
        planned_area = feature_area(bf)

        built = by_id.get(fid)
        overlap = 0.0
        if built:
            built_rings = get_rings(built.get("geometry"))
            for i in range(min(len(planned_rings), len(built_rings))):
                overlap += overlap_area(planned_rings[i], built_rings[i])
            if overlap == 0 and built_rings:
                overlap = min(feature_area(built), planned_area)

        overlap = min(overlap, planned_area)
        pct = (overlap / planned_area * 100) if planned_area > 0 else 0.0

        rows.append({
            "id": fid,
            "name": bf["properties"].get("name", fid),
            "category": bf["properties"].get("category", "Uncategorized"),
            "plannedArea": planned_area,
            "capturedArea": overlap,
            "pct": pct,
            "status": status_from_pct(pct),
            "notes": "",
            "plannedGeom": bf.get("geometry"),
            "builtGeom": built.get("geometry") if built else None,
            "fillWholePlanned": False,
        })

    return build_result(rows)


def build_result(rows: list[dict]) -> dict:
    total_planned = sum(r["plannedArea"] for r in rows)
    total_captured = sum(r["capturedArea"] for r in rows)
    counts = {"complete": 0, "progress": 0, "notstarted": 0}
    for r in rows:
        counts[r["status"]] += 1
    overall = (total_captured / total_planned * 100) if total_planned > 0 else 0.0
    return {
        "rows": rows,
        "overall": overall,
        "totalPlanned": total_planned,
        "totalCaptured": total_captured,
        "counts": counts,
    }

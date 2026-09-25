"""
vision.py

Real image-recognition analysis: sends the uploaded drone/site photo to a
vision-capable Claude model along with the list of planned blueprint
features, and asks it to assess completion per feature from what is
actually visible in the image.

This module is deliberately split into two parts:
  - build_prompt() / call_model() / parse_model_output()  -> pure functions,
    each independently unit-testable without hitting the network.
  - analyze_vision()  -> orchestrates them for the Flask route.
"""

import base64
import json
import os
import re

import requests

from geometry import feature_area, status_from_pct, build_result

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"


def build_prompt(zones: list[dict], day: str) -> str:
    zones_json = json.dumps(zones, indent=2)
    return (
        f"You are a construction/mining site progress analyst reviewing a drone "
        f"photo taken on day {day} of a project. Here are the planned site "
        f"features:\n{zones_json}\n\n"
        "Look carefully at the image and, for EACH feature id listed above, "
        "assess how complete it appears to be based on visible ground conditions "
        "(grading, compaction, staged material, standing water, paving, "
        "structures, vegetation clearance). If a feature is not visible or you "
        "cannot locate it in the frame, mark it not_started with 0 and say so in "
        "the rationale.\n\n"
        "Respond with ONLY a JSON array, no prose, no markdown fences, in "
        "exactly this shape:\n"
        '[{"id":"<feature id>","status":"not_started|in_progress|complete",'
        '"pct":<integer 0-100>,"rationale":"<one short sentence>"}]'
    )


def call_model(image_bytes: bytes, media_type: str, prompt: str, api_key: str) -> dict:
    """Makes the actual network call. Raises requests.HTTPError on failure."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": MODEL,
        "max_tokens": 1200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_image}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def parse_model_output(response_json: dict) -> list[dict]:
    """Extracts the JSON array of zone assessments from the API response."""
    blocks = response_json.get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text.strip():
        raise ValueError("model returned no text content")
    cleaned = re.sub(r"```json|```", "", text).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("model response was not a JSON array")
    return parsed


def merge_with_blueprint(blueprint: dict, zone_results: list[dict]) -> dict:
    """Combines the model's per-zone assessments with blueprint geometry/area
    to produce the same result shape analyze_geometry() returns."""
    by_id = {z["id"]: z for z in zone_results if "id" in z}

    rows = []
    for bf in blueprint.get("features", []):
        fid = bf["properties"]["id"]
        planned_area = feature_area(bf)
        vr = by_id.get(fid)

        if vr:
            pct = max(0.0, min(100.0, float(vr.get("pct", 0) or 0)))
            status = vr.get("status") or status_from_pct(pct)
            status = {"not_started": "notstarted", "in_progress": "progress"}.get(status, status)
            notes = vr.get("rationale", "")
        else:
            pct = 0.0
            status = "notstarted"
            notes = "Not addressed in model response."

        rows.append({
            "id": fid,
            "name": bf["properties"].get("name", fid),
            "category": bf["properties"].get("category", "Uncategorized"),
            "plannedArea": planned_area,
            "capturedArea": pct / 100 * planned_area,
            "pct": pct,
            "status": status,
            "notes": notes,
            "plannedGeom": bf.get("geometry"),
            "builtGeom": bf.get("geometry"),
            "fillWholePlanned": True,
        })

    return build_result(rows)


def analyze_vision(blueprint: dict, image_bytes: bytes, media_type: str, day: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy backend/.env.example to backend/.env "
            "and add your key."
        )

    zones = [
        {"id": f["properties"]["id"], "name": f["properties"].get("name"), "category": f["properties"].get("category")}
        for f in blueprint.get("features", [])
    ]
    prompt = build_prompt(zones, day)
    response_json = call_model(image_bytes, media_type, prompt, api_key)
    zone_results = parse_model_output(response_json)
    return merge_with_blueprint(blueprint, zone_results)

"""
temporal.py

Turns a history of single-point-in-time analyses into trend intelligence:
  - per-feature progress series across all captures
  - an S-curve (logistic growth) fit to that series, giving a forecasted
    completion day and a current instantaneous velocity -- construction
    progress is almost never linear (slow mobilization, fast middle,
    slow punch-list finish), and a straight-line extrapolation from the
    last two points overshoots during the fast phase and undershoots
    during the slow phases. See fit_logistic_and_forecast() for the model.
  - stall detection: zero measurable movement between the two most recent
    captures, for a feature that isn't already complete
  - schedule variance: actual % vs. a planned schedule (milestone-based or
    a single target_day fallback), flags features running meaningfully
    behind plan

All of this is pure computation over already-stored capture results — no
network calls, so it's fully unit-testable with synthetic data.
"""

import math

STALL_EPSILON = 0.5          # percentage points; smaller than this counts as "no movement"
BEHIND_SCHEDULE_MARGIN = 5.0  # percentage points behind plan before flagging
FORECAST_COMPLETION_THRESHOLD = 99.0  # % -- a logistic curve only reaches 100% at t=infinity,
                                        # so "forecasted completion" means reaching this instead


def fit_logistic_and_forecast(points: list[dict], completion_threshold: float = FORECAST_COMPLETION_THRESHOLD) -> dict:
    """
    Fits pct(t) = 100 / (1 + exp(-k*(t - t0))) to a progress series and uses
    it to forecast when the feature will reach `completion_threshold`.

    Why this instead of iterative nonlinear least squares: with L fixed at
    100 (percent complete has a known, fixed asymptote -- it cannot exceed
    100%), the logistic model linearizes exactly. Taking the logit of each
    point, logit(p) = ln(p/(100-p)), gives logit(p) = k*t - k*t0, which is
    a straight line in t. So this is fit with ordinary least squares on the
    transformed points -- closed-form, no iteration, no dependency on scipy
    or an initial-guess-sensitive optimizer, and it's exactly the maximum
    likelihood fit for this model (not an approximation of one).

    The tradeoff: points at exactly 0% or 100% are undefined in logit space
    (ln(0) and ln(inf)) and are excluded from the fit. In practice this is
    fine -- 0% points carry no information about the *shape* of the growth
    curve anyway, and a feature already at 100% doesn't need a forecast.

    Returns k, t0, an R^2 goodness-of-fit for the linearized regression, an
    instantaneous velocity estimate at the latest point (the logistic
    derivative evaluated at the current pct, which correctly slows down as
    a feature approaches completion rather than assuming a constant rate),
    and the forecasted day. Any field is None if there isn't enough usable
    data (fewer than 2 points strictly between 0-100 on distinct days) or
    if the fitted trend is flat/declining (k <= 0 -- can't forecast forward
    from a curve that isn't currently rising).
    """
    usable = [(p["day"], p["pct"]) for p in points if 0 < p["pct"] < 100]
    distinct_days = {d for d, _ in usable}

    result = {"k": None, "t0": None, "fit_r2": None, "velocity_per_week": None, "forecast_day": None}
    if len(usable) < 2 or len(distinct_days) < 2:
        return result

    ts = [d for d, _ in usable]
    ys = [math.log(p / (100 - p)) for _, p in usable]
    n = len(ts)
    mean_t = sum(ts) / n
    mean_y = sum(ys) / n
    var_t = sum((t - mean_t) ** 2 for t in ts)
    if var_t == 0:
        return result

    cov_ty = sum((t - mean_t) * (y - mean_y) for t, y in zip(ts, ys))
    k = cov_ty / var_t
    c = mean_y - k * mean_t

    y_pred = [k * t + c for t in ts]
    ss_res = sum((y - yp) ** 2 for y, yp in zip(ys, y_pred))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None

    result["k"] = k
    result["fit_r2"] = r2

    if k == 0:
        return result
    t0 = -c / k
    result["t0"] = t0

    latest_pct = points[-1]["pct"]
    p_frac = max(0.0001, min(0.9999, latest_pct / 100))
    # d(pct)/dt for the logistic curve, evaluated at the CURRENT actual pct
    # (not the fitted curve's value at the latest day, which may differ
    # slightly from the actual reading) -- this is the practically useful
    # "how fast is it moving right now" number.
    velocity_per_day = k * p_frac * (1 - p_frac) * 100
    result["velocity_per_week"] = velocity_per_day * 7

    if k > 0 and latest_pct < completion_threshold:
        result["forecast_day"] = t0 + math.log(completion_threshold / (100 - completion_threshold)) / k

    return result


def sample_logistic_curve(k: float, t0: float, day_start: float, day_end: float, num_samples: int = 40) -> list[dict]:
    """Samples the fitted S-curve between two days, for drawing the actual
    curve (not a straight-line approximation) on a chart."""
    if day_end <= day_start:
        return []
    samples = []
    for i in range(num_samples + 1):
        t = day_start + (day_end - day_start) * i / num_samples
        pct = 100 / (1 + math.exp(-k * (t - t0)))
        samples.append({"day": t, "pct": max(0.0, min(100.0, pct))})
    return samples


def planned_pct_at_day(target_day, day) -> float | None:
    """Legacy fallback: linear ramp from 0% at day 0 to 100% at target_day.
    Only used when a feature has no full milestone schedule set."""
    if not target_day or target_day <= 0:
        return None
    return max(0.0, min(100.0, day / target_day * 100))


def planned_pct_from_schedule(schedule: list, day) -> float | None:
    """
    Piecewise-linear interpolation across an explicit milestone schedule:
    a list of {"pct": <0-100>, "day": <int>} points, e.g. 10% by day 5,
    20% by day 12, ... 100% by day 50. (0%, day 0) is always an implicit
    starting point even if not given explicitly.

    This is deliberately more robust than a single target_day: one wrong
    milestone only distorts the segment around it, not the whole curve.
    """
    if not schedule:
        return None

    points = sorted(({"pct": 0.0, "day": 0}, *schedule), key=lambda p: p["day"])
    # de-duplicate same-day points, keeping the last (highest pct) one
    dedup = {}
    for p in points:
        dedup[p["day"]] = p["pct"]
    points = sorted(({"day": d, "pct": p} for d, p in dedup.items()), key=lambda p: p["day"])

    if day <= points[0]["day"]:
        return points[0]["pct"]
    if day >= points[-1]["day"]:
        return 100.0

    for i in range(len(points) - 1):
        d0, p0 = points[i]["day"], points[i]["pct"]
        d1, p1 = points[i + 1]["day"], points[i + 1]["pct"]
        if d0 <= day <= d1:
            if d1 == d0:
                return p1
            frac = (day - d0) / (d1 - d0)
            return p0 + frac * (p1 - p0)

    return 100.0  # unreachable in practice, but keeps the function total


def compute_feature_series(blueprint_feature: dict, captures: list[dict]) -> dict:
    """Builds the full time series + derived analytics for one blueprint feature."""
    fid = blueprint_feature["properties"]["id"]
    target_day = blueprint_feature["properties"].get("target_day")
    schedule = blueprint_feature["properties"].get("schedule")  # list of {"pct","day"} milestones

    points = []
    for cap in captures:
        row = next((r for r in cap["result"]["rows"] if r["id"] == fid), None)
        if row is None:
            continue
        points.append({"day": cap["day"], "pct": row["pct"], "status": row["status"]})

    points.sort(key=lambda p: p["day"])

    velocity_per_week = None
    forecast_day = None
    fit_r2 = None
    fit_k = None
    fit_t0 = None
    stalled = False
    behind_schedule = False
    variance_pct = None

    if len(points) >= 2:
        last, prev = points[-1], points[-2]
        day_delta = last["day"] - prev["day"]
        if day_delta > 0 and last["pct"] < 95 and abs(last["pct"] - prev["pct"]) < STALL_EPSILON:
            stalled = True

        if not stalled and last["pct"] < 95:
            fit = fit_logistic_and_forecast(points)
            velocity_per_week = fit["velocity_per_week"]
            forecast_day = fit["forecast_day"]
            fit_r2 = fit["fit_r2"]
            fit_k = fit["k"]
            fit_t0 = fit["t0"]

    if points:
        latest = points[-1]
        # Milestone schedule takes precedence over the legacy single target_day
        # ramp -- it's the more precise (and more typo-resilient) source of truth.
        if schedule:
            planned = planned_pct_from_schedule(schedule, latest["day"])
        else:
            planned = planned_pct_at_day(target_day, latest["day"])
        if planned is not None:
            variance_pct = latest["pct"] - planned
            if variance_pct < -BEHIND_SCHEDULE_MARGIN and latest["pct"] < 95:
                behind_schedule = True

    return {
        "id": fid,
        "name": blueprint_feature["properties"].get("name", fid),
        "category": blueprint_feature["properties"].get("category", "Uncategorized"),
        "target_day": target_day,
        "schedule": schedule,
        "points": points,
        "latest_pct": points[-1]["pct"] if points else 0,
        "latest_status": points[-1]["status"] if points else "notstarted",
        "velocity_per_week": velocity_per_week,
        "forecast_day": forecast_day,
        "forecast_fit_r2": fit_r2,
        "forecast_fit_k": fit_k,
        "forecast_fit_t0": fit_t0,
        "stalled": stalled,
        "behind_schedule": behind_schedule,
        "variance_pct": variance_pct,
    }


def compute_overall_series(captures: list[dict]) -> list[dict]:
    return [{"day": c["day"], "overall": c["result"]["overall"]} for c in sorted(captures, key=lambda c: c["day"])]


def build_timeline(site: dict, captures: list[dict]) -> dict:
    blueprint = site["blueprint"]
    feature_series = [compute_feature_series(bf, captures) for bf in blueprint.get("features", [])]

    stalled_features = [f["id"] for f in feature_series if f["stalled"]]
    behind_schedule_features = [f["id"] for f in feature_series if f["behind_schedule"]]

    # Overall project forecast: an S-curve fit to the whole-site progress series
    # (same logistic model as per-feature, applied to the aggregate).
    overall_series = compute_overall_series(captures)
    overall_velocity_per_week = None
    overall_forecast_day = None
    overall_fit_r2 = None
    overall_fit_k = None
    overall_fit_t0 = None
    if len(overall_series) >= 2:
        overall_points = [{"day": p["day"], "pct": p["overall"]} for p in overall_series]
        last, prev = overall_points[-1], overall_points[-2]
        overall_stalled = last["day"] > prev["day"] and last["pct"] < 95 and abs(last["pct"] - prev["pct"]) < STALL_EPSILON
        if not overall_stalled and last["pct"] < 95:
            fit = fit_logistic_and_forecast(overall_points)
            overall_velocity_per_week = fit["velocity_per_week"]
            overall_forecast_day = fit["forecast_day"]
            overall_fit_r2 = fit["fit_r2"]
            overall_fit_k = fit["k"]
            overall_fit_t0 = fit["t0"]

    return {
        "site_id": site["id"],
        "site_name": site["name"],
        "start_date": site.get("start_date"),
        "capture_count": len(captures),
        "overall_series": overall_series,
        "overall_velocity_per_week": overall_velocity_per_week,
        "overall_forecast_day": overall_forecast_day,
        "overall_forecast_fit_r2": overall_fit_r2,
        "overall_forecast_fit_k": overall_fit_k,
        "overall_forecast_fit_t0": overall_fit_t0,
        "features": feature_series,
        "stalled_feature_ids": stalled_features,
        "behind_schedule_feature_ids": behind_schedule_features,
    }

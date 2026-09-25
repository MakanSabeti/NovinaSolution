"""
reporting.py

Turns data the app already computes (the temporal timeline, the alert log)
into an executive-readable report: what changed over the period, what needs
attention, what's flagged. No new analysis happens here -- this is a
presentation layer over geometry.py/temporal.py/risk.py's real numbers.

Deliberately NOT LLM-generated prose for the numbers themselves -- every
figure in the report is computed the same deterministic way every time, so
two people generating a report from the same data get the same report. This
keeps it testable and keeps the numbers trustworthy, which matters more for
an executive report than a more "natural"-sounding sentence would.
"""

import datetime


def _find_baseline_point(points: list[dict], latest_day: float, period_days: int) -> dict | None:
    """Finds the point to compare 'latest' against: the most recent capture
    at or before (latest_day - period_days), or the earliest available point
    if the site's whole history is shorter than the requested period."""
    if not points:
        return None
    target_day = latest_day - period_days
    candidates = [p for p in points if p["day"] <= target_day]
    if candidates:
        return max(candidates, key=lambda p: p["day"])
    return min(points, key=lambda p: p["day"])


def generate_report_data(site: dict, timeline: dict, alerts: list[dict], period_days: int = 7) -> dict:
    """
    Builds the full report as plain data (JSON-serializable). Every number
    here is either read directly from `timeline`/`alerts` or a simple,
    hand-verifiable delta computed from them -- no new modeling.
    """
    overall_series = timeline.get("overall_series", [])
    overall_points = [{"day": p["day"], "pct": p["overall"]} for p in overall_series]
    latest_overall = overall_points[-1] if overall_points else None

    overall_baseline = _find_baseline_point(overall_points, latest_overall["day"], period_days) if latest_overall else None
    overall_delta = (latest_overall["pct"] - overall_baseline["pct"]) if (latest_overall and overall_baseline) else None

    feature_rows = []
    for f in timeline.get("features", []):
        pts = f.get("points", [])
        latest_pt = pts[-1] if pts else None
        baseline_pt = _find_baseline_point(pts, latest_pt["day"], period_days) if latest_pt else None
        delta = (latest_pt["pct"] - baseline_pt["pct"]) if (latest_pt and baseline_pt) else None
        feature_rows.append({
            "id": f["id"], "name": f["name"], "category": f["category"],
            "latest_pct": f.get("latest_pct", 0),
            "delta_over_period": delta,
            "stalled": f.get("stalled", False),
            "behind_schedule": f.get("behind_schedule", False),
            "forecast_day": f.get("forecast_day"),
        })

    top_movers = sorted(
        [r for r in feature_rows if r["delta_over_period"] is not None and r["delta_over_period"] > 0],
        key=lambda r: -r["delta_over_period"],
    )[:5]
    needs_attention = [r for r in feature_rows if r["stalled"] or r["behind_schedule"]]

    period_cutoff = datetime.datetime.now().timestamp() - period_days * 86400
    recent_alerts = [a for a in alerts if a.get("created_at", 0) >= period_cutoff]
    recent_alerts_by_severity = {"high": [], "medium": []}
    for a in recent_alerts:
        recent_alerts_by_severity.setdefault(a.get("severity", "medium"), []).append(a)

    narrative_lines = []
    if latest_overall:
        pct_str = f"{latest_overall['pct']:.1f}%"
        if overall_delta is not None:
            direction = "up" if overall_delta >= 0 else "down"
            narrative_lines.append(
                f"{site['name']} is {pct_str} complete overall, {direction} {abs(overall_delta):.1f} points "
                f"over the last {period_days} days."
            )
        else:
            narrative_lines.append(f"{site['name']} is {pct_str} complete overall.")
        if timeline.get("overall_forecast_day"):
            narrative_lines.append(f"At the current pace, the site is forecasted to reach 99% complete around day {round(timeline['overall_forecast_day'])}.")
    else:
        narrative_lines.append(f"{site['name']} has no captures yet.")

    for r in top_movers[:3]:
        narrative_lines.append(f"{r['name']} progressed {r['delta_over_period']:.1f} points this period, now at {r['latest_pct']:.1f}%.")
    for r in needs_attention:
        flag = "stalled" if r["stalled"] else "behind schedule"
        narrative_lines.append(f"{r['name']} is {flag} at {r['latest_pct']:.1f}% and needs attention.")
    if recent_alerts_by_severity.get("high"):
        narrative_lines.append(f"{len(recent_alerts_by_severity['high'])} high-severity alert(s) this period: " +
                                "; ".join(a["message"] for a in recent_alerts_by_severity["high"][:3]))

    return {
        "site_name": site["name"],
        "generated_at": datetime.datetime.now().isoformat(),
        "period_days": period_days,
        "overall_latest_pct": latest_overall["pct"] if latest_overall else None,
        "overall_delta_over_period": overall_delta,
        "overall_velocity_per_week": timeline.get("overall_velocity_per_week"),
        "overall_forecast_day": timeline.get("overall_forecast_day"),
        "capture_count": timeline.get("capture_count", 0),
        "features": feature_rows,
        "top_movers": top_movers,
        "needs_attention": needs_attention,
        "recent_alert_count": len(recent_alerts),
        "recent_alerts_by_severity": {k: len(v) for k, v in recent_alerts_by_severity.items()},
        "recent_alerts": recent_alerts[:20],
        "narrative_lines": narrative_lines,
    }


def render_report_pdf(report: dict) -> bytes:
    """Renders the report dict into a real PDF using reportlab. Kept as a
    separate function from generate_report_data() -- the data is the source
    of truth and is independently testable; this just formats it."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    navy = colors.HexColor("#1B2A4A")
    title_style = ParagraphStyle("TitleNavy", parent=styles["Title"], textColor=navy)
    h2_style = ParagraphStyle("H2Navy", parent=styles["Heading2"], textColor=navy, spaceBefore=14)

    story = [
        Paragraph(f"{report['site_name']} — Progress Report", title_style),
        Paragraph(f"Generated {report['generated_at'][:10]} &middot; last {report['period_days']} days &middot; {report['capture_count']} capture(s) on record", styles["Normal"]),
        Spacer(1, 14),
    ]

    overall_pct = report["overall_latest_pct"]
    delta = report["overall_delta_over_period"]
    summary_rows = [
        ["Overall complete", f"{overall_pct:.1f}%" if overall_pct is not None else "—"],
        ["Change this period", (f"+{delta:.1f} pts" if delta and delta >= 0 else f"{delta:.1f} pts") if delta is not None else "—"],
        ["Velocity", f"{report['overall_velocity_per_week']:.1f}%/week" if report["overall_velocity_per_week"] else "—"],
        ["Forecast (99%)", f"Day {round(report['overall_forecast_day'])}" if report["overall_forecast_day"] else "—"],
        ["Alerts this period", str(report["recent_alert_count"])],
    ]
    summary_table = Table(summary_rows, colWidths=[180, 200])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2ECE0")),
        ("TEXTCOLOR", (0, 0), (0, -1), navy),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9D0C0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Summary", h2_style))
    for line in report["narrative_lines"]:
        story.append(Paragraph(f"&bull; {line}", styles["Normal"]))
        story.append(Spacer(1, 4))

    if report["features"]:
        story.append(Paragraph("Feature Detail", h2_style))
        rows = [["Feature", "Category", "Latest %", "Δ this period", "Flags"]]
        for f in report["features"]:
            flags = []
            if f["stalled"]:
                flags.append("STALLED")
            if f["behind_schedule"]:
                flags.append("BEHIND")
            delta_str = f"{f['delta_over_period']:+.1f}" if f["delta_over_period"] is not None else "—"
            rows.append([f["name"], f["category"], f"{f['latest_pct']:.1f}%", delta_str, ", ".join(flags) or "—"])
        feature_table = Table(rows, colWidths=[130, 100, 60, 80, 90])
        feature_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), navy),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9D0C0")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(feature_table)

    if report["recent_alerts"]:
        story.append(Paragraph("Recent Alerts", h2_style))
        arows = [["Severity", "Type", "Message"]]
        for a in report["recent_alerts"]:
            arows.append([a.get("severity", "").upper(), a.get("rule_type", ""), a.get("message", "")])
        alert_table = Table(arows, colWidths=[60, 110, 290])
        alert_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), navy),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9D0C0")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(alert_table)

    doc.build(story)
    return buf.getvalue()

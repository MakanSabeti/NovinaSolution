"""
schedule_import.py

Lets a project schedule be maintained in Excel instead of typed into the UI
one feature at a time. The import format is deliberately simple: one row
per feature, one date -- when it's expected to finish. Internally, that
single date is expanded into the same 10%-milestone schedule the rest of
the app already uses (evenly spaced in time from the project start date to
the given finish date), so "behind schedule" detection and the S-curve
forecast keep working exactly as before -- the simplification is in what
the user has to type, not in the underlying model.

Workflow:
  1. GET the template for a site -> one row per blueprint feature, with an
     "Expected Finish Date" column (a real calendar date, not a day-offset).
  2. Fill in the finish date for whichever features are known and re-upload.
  3. import_schedule_workbook() matches each row back to a blueprint feature
     (by ID first, falling back to name), converts the finish date to a
     day-offset relative to the site's start date, expands it into an even
     10%-100% milestone spread, and returns the same
     {feature_id: [{"pct","day"}, ...]} shape storage.update_schedules()
     already expects.
"""

import datetime
import io

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

MILESTONE_PCTS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

NAVY = "1B2A4A"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)

FINISH_DATE_COL = "Expected Finish Date"


def _finish_day_from_schedule(schedule: list | None) -> int | None:
    """Back out an approximate finish day from an existing milestone schedule
    (the 100% entry if present, else the latest milestone) -- used only to
    pre-fill the template for a feature that already has a schedule set."""
    if not schedule:
        return None
    hundred = next((m["day"] for m in schedule if m["pct"] == 100), None)
    if hundred is not None:
        return hundred
    return max(m["day"] for m in schedule)


def build_template_workbook(site: dict) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()

    ws_info = wb.active
    ws_info.title = "Read Me"
    ws_info.column_dimensions["A"].width = 95
    ws_info["A1"] = f"Schedule template — {site['name']}"
    ws_info["A1"].font = Font(bold=True, size=13, color=NAVY)
    start_date = site.get("start_date") or "NOT SET — set a start date for this site before importing"
    lines = [
        "",
        f"Project start date: {start_date}",
        "",
        "Fill in the 'Schedule' sheet: one row per feature is already listed. For each feature, enter",
        "the calendar date it's expected to finish (100% complete) in the 'Expected Finish Date' column.",
        "Leave it blank to skip that feature -- you don't need every row filled in.",
        "",
        "That single date is enough: Stratum spreads it into a 10%/20%/.../100% pace schedule",
        "automatically (evenly spaced between the project start date and the finish date you give),",
        "so 'behind schedule' tracking and the forecast still work the same way they do for a",
        "manually-entered schedule -- you just don't have to type ten dates per feature to get it.",
        "",
        "Use real calendar dates (e.g. 2026-03-15), not day numbers.",
        "",
        "Don't edit the 'Feature ID' column — that's how each row gets matched back to the right",
        "feature when you re-upload this file. The 'Feature Name' column is just for reference.",
    ]
    for i, line in enumerate(lines, start=2):
        ws_info[f"A{i}"] = line

    ws = wb.create_sheet("Schedule", 1)
    headers = ["Feature ID", "Feature Name", "Category", FINISH_DATE_COL]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    for f in site["blueprint"].get("features", []):
        props = f["properties"]
        finish_day = _finish_day_from_schedule(props.get("schedule"))
        finish_date_str = None
        if finish_day is not None and site.get("start_date"):
            d = datetime.date.fromisoformat(site["start_date"]) + datetime.timedelta(days=finish_day)
            finish_date_str = d.isoformat()
        ws.append([props["id"], props.get("name", props["id"]), props.get("category", ""), finish_date_str])

    widths = [12, 26, 18, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    return wb


def workbook_to_bytes(wb: openpyxl.Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ScheduleImportError(Exception):
    pass


def _parse_date_cell(value) -> datetime.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip())
        except ValueError:
            raise ScheduleImportError(f"'{value}' is not a recognizable date (expected YYYY-MM-DD or an Excel date cell).")
    raise ScheduleImportError(f"Unrecognized date value: {value!r}")


def expand_finish_day_to_milestones(finish_day: int) -> list:
    """Evenly spreads a single finish day into the 10%-100% milestone shape,
    e.g. finish_day=30 -> 10% by day 3, 20% by day 6, ... 100% by day 30."""
    return [{"pct": p, "day": round(finish_day * p / 100)} for p in MILESTONE_PCTS]


def import_schedule_workbook(file_bytes: bytes, blueprint_features: list, start_date: str) -> dict:
    """
    Returns {"schedules": {feature_id: [{"pct","day"}, ...]}, "warnings": [...]}.
    Raises ScheduleImportError for problems that make the whole import unsafe
    to apply (missing sheet, unparseable date, no start date); collects
    per-row issues (unmatched feature, a finish date before the start date)
    as warnings instead, since those shouldn't block importing the rows that
    ARE fine.
    """
    if not start_date:
        raise ScheduleImportError("This site has no start date set. Set one before importing a schedule.")
    try:
        project_start = datetime.date.fromisoformat(start_date)
    except ValueError:
        raise ScheduleImportError(f"Site start date '{start_date}' is not a valid date.")

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as e:
        raise ScheduleImportError(f"Could not read this file as an Excel workbook: {e}")

    if "Schedule" not in wb.sheetnames:
        raise ScheduleImportError("No 'Schedule' sheet found. Use the downloaded template rather than a reformatted copy.")
    ws = wb["Schedule"]

    header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    expected = ["Feature ID", "Feature Name", "Category", FINISH_DATE_COL]
    if header_row[:4] != expected:
        raise ScheduleImportError(
            "The 'Schedule' sheet's columns don't match the expected template layout "
            f"(expected {expected}, got {header_row[:4]})."
        )

    by_id = {f["properties"]["id"]: f for f in blueprint_features}
    by_name = {f["properties"].get("name", "").strip().lower(): f for f in blueprint_features}

    schedules = {}
    warnings = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(v is None for v in row):
            continue
        fid, fname = row[0], row[1]
        feature = None
        if fid and fid in by_id:
            feature = by_id[fid]
        elif fname and str(fname).strip().lower() in by_name:
            feature = by_name[str(fname).strip().lower()]
            warnings.append(f"Row {row_idx}: matched by name ('{fname}') — Feature ID column was blank or didn't match.")
        else:
            warnings.append(f"Row {row_idx}: no feature matches ID '{fid}' or name '{fname}' — row skipped.")
            continue

        finish_date = _parse_date_cell(row[3])
        if finish_date is None:
            warnings.append(f"Row {row_idx} ({fname}): no finish date given — no schedule set for this feature.")
            continue

        finish_day = (finish_date - project_start).days
        if finish_day <= 0:
            warnings.append(f"Row {row_idx} ({fname}): finish date is on or before the project start date — skipped.")
            continue

        schedules[feature["properties"]["id"]] = expand_finish_day_to_milestones(finish_day)

    if not schedules:
        raise ScheduleImportError("No usable finish dates found in any row — nothing was imported.")

    return {"schedules": schedules, "warnings": warnings}

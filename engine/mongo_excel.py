"""Generate an xlsx workbook on demand from Mongo run_raw logs.

Simpler than the engine-driven monthly-per-file format: one workbook
per run, with each log as its own sheet. Good enough for the Cowork
tutorial (Claude reads these sheets and builds aggregation scripts).
"""
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import mongo_runs


HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="333333")


def _write_sheet(ws, rows, title):
    ws.title = title
    if not rows:
        ws.append([f"(no {title.lower()} in this run)"])
        return
    # Gather column union across all rows so sparse logs don't drop keys
    columns = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)
    ws.append(columns)
    for c, cell in enumerate(ws[1], start=1):
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for r in rows:
        ws.append([_coerce(r.get(k)) for k in columns])
    # Freeze header + auto-size-ish
    ws.freeze_panes = "A2"
    for col_idx, key in enumerate(columns, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(
            12, min(30, len(str(key)) + 2)
        )


def _coerce(v):
    if isinstance(v, (dict, list)):
        import json as _json
        return _json.dumps(v, default=str)
    return v


def run_to_xlsx_bytes(run_id):
    """Build an xlsx workbook from the run_raw document for a given run_id.
    Returns (bytes, filename) or (None, None) if the run doesn't exist."""
    raw = mongo_runs.get_run_raw(run_id)
    summary = mongo_runs.get_run_summary(run_id)
    if not raw or not summary:
        return None, None

    wb = Workbook()

    # First sheet: Summary
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Field", "Value"])
    for c in ws[1]:
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    ws.append(["label", summary.get("label", "")])
    ws.append(["months", summary.get("months", "")])
    ws.append(["bot", summary.get("bot_slug", "")])
    ws.append(["started_at", str(summary.get("started_at", ""))])
    for k, v in (summary.get("summary") or {}).items():
        ws.append([k, v])
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 20

    # One sheet per log
    for sheet_title, key in [
        ("Sales", "order_log"),
        ("Purchase_Orders", "po_log"),
        ("Transfers", "transfer_log"),
        ("Stock_Snapshot", "daily_stock_log"),
        ("Financials", "financial_log"),
        ("Action_Log", "action_log"),
        ("Shelf_Layout", "shelf_layout"),
        ("Shelf_Assignments_Initial", "shelf_assignments_initial"),
        ("Shelf_Assignments_Final", "shelf_assignments_final"),
        ("Trend_Events", "trend_events"),
    ]:
        ws = wb.create_sheet(sheet_title)
        _write_sheet(ws, raw.get(key, []) or [], sheet_title)

    buf = io.BytesIO()
    wb.save(buf)

    label = summary.get("label", "run")
    filename = f"toyland-{label}-{str(run_id)}.xlsx"
    return buf.getvalue(), filename

"""Thin Google Sheets API wrappers for the tracker (all planning stays in rows.py).

Three operations: make sure the two tabs exist (with their one-time cosmetic setup —
status dropdown, conditional formatting, hidden dedupe-key column, frozen header),
read a tab snapshot, and apply a ``SheetPlan``. Formulas are written with
``valueInputOption=USER_ENTERED`` so ``=HYPERLINK(...)`` cells render as links.
"""

from __future__ import annotations

from typing import Any

from ..logging_config import get_logger
from .rows import ANSWERS_HEADERS, COL_KEY, COL_STATUS, HEADERS, STATUS_OPTIONS, SheetPlan

log = get_logger(__name__)

APPLICATIONS_TAB = "Applications"
ANSWERS_TAB = "Answers"

# Status → background color for the one-time conditional formatting (cosmetic).
_STATUS_COLORS: dict[str, dict[str, float]] = {
    "offer": {"red": 0.80, "green": 0.94, "blue": 0.80},
    "interviewing": {"red": 0.99, "green": 0.95, "blue": 0.78},
    "submitted": {"red": 0.82, "green": 0.89, "blue": 0.98},
    "rejected": {"red": 0.90, "green": 0.90, "blue": 0.90},
    "withdrawn": {"red": 0.90, "green": 0.90, "blue": 0.90},
}


def _col_letter(col: int) -> str:
    return chr(ord("A") + col)  # the tracker never exceeds 26 columns


def _applications_setup_requests(sheet_id: int) -> list[dict]:
    """One-time setup for a freshly-created Applications tab."""
    requests: list[dict] = [
        # Freeze the header row.
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Status dropdown (data validation) on every data row.
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": COL_STATUS,
                    "endColumnIndex": COL_STATUS + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": s} for s in STATUS_OPTIONS],
                    },
                    "showCustomUi": True,
                    "strict": False,  # the human may type something else; don't fight them
                },
            }
        },
        # Hide the dedupe-key column (the upsert identity — not for humans).
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": COL_KEY,
                    "endIndex": COL_KEY + 1,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        },
    ]
    # Per-status row coloring (green offer, grey rejected, ...).
    status_col = _col_letter(COL_STATUS)
    for index, (status, color) in enumerate(_STATUS_COLORS.items()):
        requests.append(
            {
                "addConditionalFormatRule": {
                    "index": index,
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(HEADERS),
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [
                                    {"userEnteredValue": f'=${status_col}2="{status}"'}
                                ],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                }
            }
        )
    return requests


def _answers_setup_requests(sheet_id: int) -> list[dict]:
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        }
    ]


def ensure_tracker_tabs(sheets: Any, spreadsheet_id: str) -> dict[str, int]:
    """Create the Applications/Answers tabs (with headers + cosmetics) if missing.

    Idempotent: existing tabs are left completely alone (their formatting may have
    been hand-tuned). Returns ``{tab title: sheetId}`` for both tabs.
    """
    meta = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    tab_ids: dict[str, int] = {
        s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])
    }

    for tab, headers, setup in (
        (APPLICATIONS_TAB, HEADERS, _applications_setup_requests),
        (ANSWERS_TAB, ANSWERS_HEADERS, _answers_setup_requests),
    ):
        if tab in tab_ids:
            continue
        resp = (
            sheets.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            )
            .execute()
        )
        sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        tab_ids[tab] = sheet_id
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": setup(sheet_id)}
        ).execute()
        log.info("created tracker tab", extra={"tab": tab, "sheet_id": sheet_id})
    return tab_ids


def read_rows(sheets: Any, spreadsheet_id: str, tab: str) -> list[list[str]]:
    """The tab's current values (header included), as the planner expects them."""
    resp = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{tab}'")
        .execute()
    )
    return [[str(c) for c in row] for row in resp.get("values", [])]


def apply_plan(sheets: Any, spreadsheet_id: str, tab: str, plan: SheetPlan) -> None:
    """Apply a ``SheetPlan``: batch the blank-cell fills, then append the new rows."""
    if plan.updates:
        data = [
            {
                "range": f"'{tab}'!{_col_letter(u.col)}{u.row}",
                "values": [[u.value]],
            }
            for u in plan.updates
        ]
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
    if plan.appends:
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": plan.appends},
        ).execute()

"""Thin Sheets wrapper for the Networking tab (all planning stays in rows.py).

Mirrors ``tracker/sheets.py`` (whose generic ``read_rows``/``apply_plan``/
``delete_rows`` this tab reuses): one function makes sure the tab exists with
its one-time cosmetics — status dropdown, per-status row colors, hidden
person-id column, frozen header — and, like the tracker, re-applies the Status
dropdown on every sync so it self-heals.
"""

from __future__ import annotations

from typing import Any

from ..logging_config import get_logger
from .rows import COL_KEY, COL_STATUS, HEADERS, STATUS_OPTIONS

log = get_logger(__name__)

NETWORKING_TAB = "Networking"

# Status → background color (cosmetic): green wins, amber action-needed, grey stalled.
_STATUS_COLORS: dict[str, dict[str, float]] = {
    "replied": {"red": 0.80, "green": 0.94, "blue": 0.80},
    "accepted": {"red": 0.85, "green": 0.93, "blue": 0.83},
    "connect_drafted": {"red": 0.99, "green": 0.95, "blue": 0.78},
    "message_drafted": {"red": 0.99, "green": 0.95, "blue": 0.78},
    "email_due": {"red": 0.95, "green": 0.87, "blue": 0.80},
}


def _col_letter(col: int) -> str:
    return chr(ord("A") + col)  # the tab never exceeds 26 columns


def _status_dropdown_request(sheet_id: int) -> dict:
    return {
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
    }


def _setup_requests(sheet_id: int) -> list[dict]:
    requests: list[dict] = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        _status_dropdown_request(sheet_id),
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
                                "values": [{"userEnteredValue": f'=${status_col}2="{status}"'}],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                }
            }
        )
    return requests


def ensure_networking_tab(sheets: Any, spreadsheet_id: str) -> int:
    """Create the Networking tab (headers + cosmetics) if missing; return its sheetId.

    Idempotent: an existing tab keeps its formatting, except the Status dropdown,
    which is re-applied every call (same self-healing rule as the tracker tabs).
    """
    meta = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    tab_ids: dict[str, int] = {
        s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])
    }
    if NETWORKING_TAB in tab_ids:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [_status_dropdown_request(tab_ids[NETWORKING_TAB])]},
        ).execute()
        return tab_ids[NETWORKING_TAB]

    resp = (
        sheets.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": NETWORKING_TAB}}}]},
        )
        .execute()
    )
    sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{NETWORKING_TAB}'!A1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": _setup_requests(sheet_id)}
    ).execute()
    log.info("created networking tab", extra={"tab": NETWORKING_TAB, "sheet_id": sheet_id})
    return sheet_id

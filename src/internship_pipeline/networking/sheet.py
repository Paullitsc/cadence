"""Thin Sheets wrapper for the Networking tab (all planning stays in rows.py).

Mirrors ``tracker/sheets.py`` (whose generic ``read_rows``/``apply_plan``/
``delete_rows`` this tab reuses): one function makes sure the tab exists with
its one-time cosmetics — status dropdown, per-status row colors, hidden
person-id column, frozen header — and, like the tracker, re-applies the Status
dropdown on every sync so it self-heals.

The tab is also re-sorted on every sync (``sort_networking_rows``). Unlike the
tracker, this tab is *read* by the human company by company, and new rows are
appended at the bottom by the Sheets API — so the second person added at an
already-listed company lands ~160 rows below the first and reads as "the
pipeline only picked up one person". Sorting is the fix, and it is idempotent.
"""

from __future__ import annotations

from typing import Any

from ..logging_config import get_logger
from .rows import COL_COMPANY, COL_KEY, COL_STATUS, COL_TIER, HEADERS, STATUS_OPTIONS

log = get_logger(__name__)

NETWORKING_TAB = "Networking"

# Status → background color (cosmetic): green wins, amber action-needed, grey stalled.
_STATUS_COLORS: dict[str, dict[str, float]] = {
    "replied": {"red": 0.80, "green": 0.94, "blue": 0.80},
    "accepted": {"red": 0.85, "green": 0.93, "blue": 0.83},
    "connect_drafted": {"red": 0.99, "green": 0.95, "blue": 0.78},
    "message_drafted": {"red": 0.99, "green": 0.95, "blue": 0.78},
    "email_drafted": {"red": 0.99, "green": 0.95, "blue": 0.78},
    "email_due": {"red": 0.95, "green": 0.87, "blue": 0.80},
    "email_sent": {"red": 0.88, "green": 0.90, "blue": 0.95},
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


def sort_request(sheet_id: int) -> dict:
    """Re-sort the data rows into tier → company → person order.

    New people are added with ``values().append``, which can only put a row at
    the BOTTOM of the tab. That is wrong for this tab specifically: a company's
    people have to sit together, or the human reading a company's row concludes
    the pipeline picked up only the first of the people they listed.

    Sorting by ``Person id`` last keeps a company's people in the order the
    targets file lists them (the id is positional), and — because the id is
    ``<campaign>-<company-slug>-<n>`` — it also groups the company even when the
    Company cell is a ``=HYPERLINK()`` formula. Past nine people at one company
    the text sort puts ``-10`` before ``-2``; they stay grouped, which is the
    point. Row-level: the range is unbounded in columns so Notes (and anything
    the human parked further right) travel with their row, and unbounded below
    so the blank tail of the grid simply sorts last.
    """
    return {
        "sortRange": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1},
            "sortSpecs": [
                {"dimensionIndex": COL_TIER, "sortOrder": "ASCENDING"},
                {"dimensionIndex": COL_COMPANY, "sortOrder": "ASCENDING"},
                {"dimensionIndex": COL_KEY, "sortOrder": "ASCENDING"},
            ],
        }
    }


def sort_networking_rows(sheets: Any, spreadsheet_id: str, sheet_id: int) -> None:
    """Apply :func:`sort_request` — called at the END of every sync.

    Every run, not only when rows were appended: it is idempotent (an ordered
    tab sorts to itself), it repairs a tab whose rows were appended out of order
    by earlier runs, and it survives the human dragging rows around.
    """
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": [sort_request(sheet_id)]}
    ).execute()


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

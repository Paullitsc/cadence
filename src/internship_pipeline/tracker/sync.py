"""One shared sheet-sync routine: project applications into the tracker tabs.

Used by the daily ``sync_tracker`` stage, the review app's submit (one row,
immediately), and the backfill command — same upsert rules everywhere: rows keyed
by the hidden dedupe-key column, human-owned cells never overwritten, Answers tab
appended first so the Applications tab can link to the anchor rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..logging_config import get_logger
from ..models import Application
from .auth import TrackerServices
from .rows import plan_answers_upsert, plan_applications_upsert
from .sheets import (
    ANSWERS_TAB,
    APPLICATIONS_TAB,
    apply_plan,
    ensure_tracker_tabs,
    read_rows,
)

log = get_logger(__name__)


@dataclass
class SyncOutcome:
    rows_appended: int = 0
    cells_filled: int = 0
    answer_rows_appended: int = 0


def sync_applications_to_sheet(
    services: TrackerServices,
    spreadsheet_id: str,
    apps: list[Application],
    *,
    locations_by_key: dict[str, list[str]] | None = None,
    cv_links_by_key: dict[str, str] | None = None,
) -> SyncOutcome:
    """Upsert ``apps`` into the tracker spreadsheet (tabs created if missing)."""
    tab_ids = ensure_tracker_tabs(services.sheets, spreadsheet_id)

    # Answers tab first: its final row numbers are what the Applications tab links to.
    answers_plan, answers_anchors = plan_answers_upsert(
        read_rows(services.sheets, spreadsheet_id, ANSWERS_TAB), apps
    )
    apply_plan(services.sheets, spreadsheet_id, ANSWERS_TAB, answers_plan)

    plan = plan_applications_upsert(
        read_rows(services.sheets, spreadsheet_id, APPLICATIONS_TAB),
        apps,
        prepared_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        locations_by_key=locations_by_key,
        answers_gid=tab_ids.get(ANSWERS_TAB),
        answers_rows=answers_anchors,
        cv_links_by_key=cv_links_by_key,
    )
    apply_plan(services.sheets, spreadsheet_id, APPLICATIONS_TAB, plan)
    return SyncOutcome(
        rows_appended=len(plan.appends),
        cells_filled=len(plan.updates),
        answer_rows_appended=len(answers_plan.appends),
    )

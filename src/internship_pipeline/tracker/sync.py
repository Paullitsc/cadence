"""One shared sheet-sync routine: project applications into the tracker tabs.

Used by the daily ``sync_tracker`` stage, the review app's submit (one row,
immediately), and the backfill command — same upsert rules everywhere: rows keyed
by the hidden dedupe-key column, human-owned cells never overwritten, Answers tab
appended first so the Applications tab can link to the anchor rows.

Human rejections are honored FIRST: any row whose Status the human set to
``rejected`` is recorded in storage (application status → ``rejected``, so no
later sync resurrects it) and then deleted from the sheet. Only after that is the
upsert planned, against the post-deletion snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..logging_config import get_logger
from ..models import Application
from ..storage import Storage
from .auth import TrackerServices
from .rows import plan_answers_upsert, plan_applications_upsert, plan_rejected_removals
from .sheets import (
    ANSWERS_TAB,
    APPLICATIONS_TAB,
    apply_plan,
    delete_rows,
    ensure_tracker_tabs,
    read_rows,
)

log = get_logger(__name__)


@dataclass
class SyncOutcome:
    rows_appended: int = 0
    cells_filled: int = 0
    answer_rows_appended: int = 0
    rows_removed: int = 0  # human set Status to "rejected" → row deleted


def _process_rejections(
    services: TrackerServices,
    spreadsheet_id: str,
    applications_sheet_id: int,
    existing: list[list[str]],
    storage: Storage,
) -> set[str]:
    """Mark human-rejected rows in storage, then delete them from the sheet.

    Storage first: if the deletion then fails, the next sync finds the row (its
    Status still reads "rejected") and retries; the reverse order could delete a
    row whose rejection was never recorded, and the daily reconcile would
    re-append it. Returns the rejected dedupe keys.
    """
    row_numbers, keys = plan_rejected_removals(existing)
    if not row_numbers:
        return set()
    for key in keys:
        try:
            app = storage.get_application(key)
            if app is not None and app.status != "rejected":
                app.status = "rejected"
                storage.save_application(app)
        except Exception as exc:  # the row still gets deleted; next sync re-records
            log.warning(
                "could not record rejection in storage",
                extra={"key": key, "error": repr(exc)},
            )
    delete_rows(services.sheets, spreadsheet_id, applications_sheet_id, row_numbers)
    log.info("removed human-rejected rows from the sheet", extra={"rows": len(row_numbers)})
    return set(keys)


def sync_applications_to_sheet(
    services: TrackerServices,
    spreadsheet_id: str,
    apps: list[Application],
    *,
    storage: Storage,
    locations_by_key: dict[str, list[str]] | None = None,
    cv_links_by_key: dict[str, str] | None = None,
) -> SyncOutcome:
    """Upsert ``apps`` into the tracker spreadsheet (tabs created if missing),
    after honoring any Status the human set to ``rejected`` (row removed from the
    sheet, application marked rejected in ``storage``)."""
    tab_ids = ensure_tracker_tabs(services.sheets, spreadsheet_id)

    existing = read_rows(services.sheets, spreadsheet_id, APPLICATIONS_TAB)
    rejected_keys = _process_rejections(
        services, spreadsheet_id, tab_ids[APPLICATIONS_TAB], existing, storage
    )
    if rejected_keys:
        apps = [a for a in apps if a.dedupe_key not in rejected_keys]
        # Deletions shifted every row below them — re-snapshot before planning.
        existing = read_rows(services.sheets, spreadsheet_id, APPLICATIONS_TAB)

    # Answers tab first: its final row numbers are what the Applications tab links to.
    answers_plan, answers_anchors = plan_answers_upsert(
        read_rows(services.sheets, spreadsheet_id, ANSWERS_TAB), apps
    )
    apply_plan(services.sheets, spreadsheet_id, ANSWERS_TAB, answers_plan)

    plan = plan_applications_upsert(
        existing,
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
        rows_removed=len(rejected_keys),
    )

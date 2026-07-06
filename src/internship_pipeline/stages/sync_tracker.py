"""Stage: sync the Google Sheets application tracker (Phase 5).

Projects prepared applications into the tracker spreadsheet — the human's
application workspace. Storage stays the source of truth; the sheet is a
projection. Reads this run's rows from ``ctx.data["prepared"]`` and reconciles
older ``pending_review`` applications from storage, so rows that exist in storage
but not yet in the sheet get appended (e.g. after enabling the tracker mid-way).

Upsert is idempotent by the hidden dedupe-key column and NEVER overwrites what the
human owns: Notes is never written after the initial insert, Status is written once
as "prepared", and every other pipeline-owned cell is only filled while blank.

Zero-credential behavior: with the tracker unconfigured this logs one line and
no-ops (the pipeline must run end-to-end with zero credentials).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..logging_config import get_logger
from ..models import Application, StageContext, StageResult
from ..storage import get_storage
from ..tracker import build_tracker_services, plan_answers_upsert, plan_applications_upsert
from ..tracker.sheets import (
    ANSWERS_TAB,
    APPLICATIONS_TAB,
    apply_plan,
    ensure_tracker_tabs,
    read_rows,
)

NAME = "sync_tracker"

log = get_logger(__name__)


def _collect_applications(ctx: StageContext) -> tuple[list[Application], dict[str, list[str]]]:
    """This run's prepared applications first (freshest), then older pending ones.

    Also returns job locations by dedupe key — known only for this run's jobs (the
    Application row doesn't carry locations); older reconciled rows leave the cell
    blank for the human.
    """
    prepared = ctx.data.get("prepared", [])
    apps: list[Application] = [item.app for item in prepared]
    locations = {item.job.dedupe_key(): item.job.locations for item in prepared}

    seen = {a.dedupe_key for a in apps}
    storage = get_storage(ctx.settings)
    try:
        for app in storage.list_applications(status="pending_review"):
            if app.dedupe_key not in seen:
                seen.add(app.dedupe_key)
                apps.append(app)
    except Exception as exc:  # reconcile is best-effort; this run's rows still sync
        log.warning(
            "could not reconcile stored applications; syncing this run only",
            extra={"run_id": ctx.run_id, "error": repr(exc)},
        )
    finally:
        storage.close()
    return apps, locations


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    services = build_tracker_services(s)  # logs the one "not configured" line itself
    if services is None:
        return StageResult(
            name=NAME,
            counts={"tracker_rows_appended": 0, "tracker_cells_filled": 0},
            notes="tracker not configured",
        )

    apps, locations_by_key = _collect_applications(ctx)
    if not apps:
        log.info("no applications to sync", extra={"run_id": ctx.run_id})
        return StageResult(
            name=NAME, counts={"tracker_rows_appended": 0, "tracker_cells_filled": 0}
        )

    spreadsheet_id = s.sheets_spreadsheet_id or ""
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
    )
    apply_plan(services.sheets, spreadsheet_id, APPLICATIONS_TAB, plan)

    counts = {
        "tracker_rows_appended": len(plan.appends),
        "tracker_cells_filled": len(plan.updates),
        "tracker_answer_rows_appended": len(answers_plan.appends),
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"synced={len(apps)}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

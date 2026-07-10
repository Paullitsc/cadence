"""Stage: sync the Google Sheets application tracker (Phase 5 + review gating).

Projects REVIEWED applications into the tracker spreadsheet — the human's
application workspace. An application reaches the sheet only after the human has
confirmed its CV in the local review app (``python -m internship_pipeline.review``),
which normally pushes the row itself the moment it's submitted; this stage is the
daily reconcile for reviewed rows that never made it (e.g. the tracker was
unconfigured or offline at submit time) and for blank cells that can be filled now.
Storage stays the source of truth; the sheet is a projection.

Upsert is idempotent by the hidden dedupe-key column and NEVER overwrites what the
human owns: Notes is never written after the initial insert, Status is written once
as "prepared", and every other pipeline-owned cell is only filled while blank —
except the CV cell, which always tracks the latest Drive link.

Zero-credential behavior: with the tracker unconfigured this logs one line and
no-ops (the pipeline must run end-to-end with zero credentials).
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import Application, StageContext, StageResult
from ..tracker import build_tracker_services
from ..tracker.sheets import ensure_tracker_tabs
from ..tracker.sync import sync_applications_to_sheet

NAME = "sync_tracker"

log = get_logger(__name__)


def _collect_applications(
    ctx: StageContext,
) -> tuple[list[Application], dict[str, list[str]], dict[str, str]]:
    """Reviewed applications, their job locations, and every stored Drive link.

    Freshly-prepared (``pending_review``) applications are deliberately NOT
    collected — they wait for the human's CV review. Locations are joined from
    the jobs table (best-effort). The third value maps EVERY stored application's
    dedupe key to its Drive CV link (any status), so the upsert can say "same as
    row N" even when the row holding the link is past pending.
    """
    storage = ctx.get_storage()
    apps = storage.list_applications(status="reviewed")

    locations: dict[str, list[str]] = {}
    for app in apps:
        try:
            job = storage.get_job(app.dedupe_key)
        except Exception:  # locations are cosmetic; never fail the sync over them
            job = None
        if job is not None and job.locations:
            locations[app.dedupe_key] = job.locations

    cv_links: dict[str, str] = {}
    try:
        for app in storage.list_applications():
            if app.cv_drive_link:
                cv_links.setdefault(app.dedupe_key, app.cv_drive_link)
    except Exception as exc:  # best-effort; the reviewed rows still sync
        log.warning(
            "could not list stored applications for CV-link join",
            extra={"run_id": ctx.run_id, "error": repr(exc)},
        )
    return apps, locations, cv_links


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

    spreadsheet_id = s.sheets_spreadsheet_id or ""

    apps, locations_by_key, cv_links_by_key = _collect_applications(ctx)
    if not apps:
        # Still worth the round trip: keeps tab cosmetics — chiefly the Status
        # dropdown — self-healing daily instead of only on the next review submit
        # (sync_applications_to_sheet, which normally does this, is never reached
        # below when there's nothing to sync).
        ensure_tracker_tabs(services.sheets, spreadsheet_id)
        log.info(
            "no reviewed applications to sync (review pending ones via "
            "`python -m internship_pipeline.review`)",
            extra={"run_id": ctx.run_id},
        )
        return StageResult(
            name=NAME, counts={"tracker_rows_appended": 0, "tracker_cells_filled": 0}
        )

    outcome = sync_applications_to_sheet(
        services,
        spreadsheet_id,
        apps,
        locations_by_key=locations_by_key,
        cv_links_by_key=cv_links_by_key,
    )

    counts = {
        "tracker_rows_appended": outcome.rows_appended,
        "tracker_cells_filled": outcome.cells_filled,
        "tracker_answer_rows_appended": outcome.answer_rows_appended,
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"synced={len(apps)}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

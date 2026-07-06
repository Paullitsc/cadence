"""One-off backfill: recover the CVs the CI runs already prepared and lost.

    python -m internship_pipeline.tracker.backfill            # do it
    python -m internship_pipeline.tracker.backfill --dry-run  # report only

Tailored PDFs used to be rendered on the ephemeral GitHub Actions runner and
destroyed with it — only ``tailored_resume_yaml`` survived (in storage). This
command re-renders every stored application's PDF locally from that YAML (free —
no LLM call), uploads it to the shared Drive folder, saves the ``cv_drive_link``
back onto the application row, and syncs the tracker sheet. Requires the tracker
to be configured (see ACTIONS_FOR_PAUL.md); RenderCV must be installed for the
PDF step (``uv sync --extra render``).

Idempotent: applications that already have a ``cv_drive_link`` are skipped, and
Drive uploads update-by-name rather than duplicating.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import yaml

from ..config import get_settings
from ..logging_config import configure_logging, get_logger
from ..models import Application
from ..resume.rendercv import write_and_render
from ..storage import Storage, get_storage
from ..tracker import build_tracker_services, plan_answers_upsert, plan_applications_upsert
from ..tracker.drive import upload_pdf
from ..tracker.sheets import (
    ANSWERS_TAB,
    APPLICATIONS_TAB,
    apply_plan,
    ensure_tracker_tabs,
    read_rows,
)

log = get_logger(__name__)


def needs_backfill(app: Application) -> bool:
    """An application we can recover: has the auditable YAML but no durable link."""
    return bool(app.tailored_resume_yaml) and not (app.cv_drive_link or "").strip()


def backfill_application(app: Application, *, settings, storage: Storage, drive) -> bool:
    """Re-render one application's PDF from stored YAML and upload it. True on success."""
    try:
        cv_doc = yaml.safe_load(app.tailored_resume_yaml or "")
    except yaml.YAMLError as exc:
        log.warning("stored YAML unparseable; skipping", extra={"key": app.dedupe_key, "error": repr(exc)})
        return False
    if not isinstance(cv_doc, dict):
        return False

    yaml_path, pdf_path = write_and_render(cv_doc, settings.resume_output_dir, app.dedupe_key)
    if pdf_path is None:
        log.warning(
            "PDF render unavailable (install the 'render' extra); skipping",
            extra={"key": app.dedupe_key, "yaml": yaml_path},
        )
        return False

    uploaded = upload_pdf(drive, settings.drive_folder_id, pdf_path, f"{app.dedupe_key}.pdf")
    if uploaded is None:
        return False

    app.tailored_resume_path = pdf_path
    app.cv_drive_link = uploaded.web_view_link
    storage.save_application(app)
    log.info("backfilled CV", extra={"key": app.dedupe_key, "link": app.cv_drive_link})
    return True


def sync_sheet(apps: list[Application], *, settings, services) -> tuple[int, int]:
    """Project ``apps`` into the tracker sheet (same upsert rules as the daily stage)."""
    spreadsheet_id = settings.sheets_spreadsheet_id or ""
    tab_ids = ensure_tracker_tabs(services.sheets, spreadsheet_id)

    answers_plan, anchors = plan_answers_upsert(
        read_rows(services.sheets, spreadsheet_id, ANSWERS_TAB), apps
    )
    apply_plan(services.sheets, spreadsheet_id, ANSWERS_TAB, answers_plan)

    plan = plan_applications_upsert(
        read_rows(services.sheets, spreadsheet_id, APPLICATIONS_TAB),
        apps,
        prepared_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        answers_gid=tab_ids.get(ANSWERS_TAB),
        answers_rows=anchors,
    )
    apply_plan(services.sheets, spreadsheet_id, APPLICATIONS_TAB, plan)
    return len(plan.appends), len(plan.updates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover lost CV PDFs into Drive + the tracker sheet.")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen; change nothing")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(args.log_level or settings.log_level)

    services = build_tracker_services(settings)
    if services is None:
        print(
            "Tracker not configured — set TRACKER_SHEETS_ENABLED=true, "
            "GOOGLE_SERVICE_ACCOUNT_JSON, SHEETS_SPREADSHEET_ID, DRIVE_FOLDER_ID "
            "(see ACTIONS_FOR_PAUL.md)."
        )
        return 1
    if not settings.drive_folder_id:
        print("DRIVE_FOLDER_ID is unset — nowhere to upload the recovered PDFs.")
        return 1

    storage = get_storage(settings)
    try:
        apps = storage.list_applications()
        todo = [a for a in apps if needs_backfill(a)]
        print(f"{len(apps)} stored applications; {len(todo)} need a Drive CV.")
        if args.dry_run:
            for app in todo:
                print(f"  would backfill: {app.dedupe_key}  {app.company_name} — {app.title}")
            return 0

        recovered = 0
        for app in todo:
            if backfill_application(app, settings=settings, storage=storage, drive=services.drive):
                recovered += 1
        appended, filled = sync_sheet(apps, settings=settings, services=services)
        print(
            f"Recovered {recovered}/{len(todo)} CVs to Drive; sheet: "
            f"{appended} rows appended, {filled} cells filled."
        )
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

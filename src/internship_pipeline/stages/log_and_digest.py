"""Stage: log & digest (Phase 1 file; Phase 4 morning email; Phase 5 outreach focus).

Assemble the daily digest — a compact count header with one link to the Google
Sheet tracker (the application workspace now lives THERE, not in the email),
outreach drafts awaiting approval (including their Gmail-draft links), and possible
recruiter replies — WRITE it to a local HTML file, and, when ``DIGEST_EMAIL_ENABLED``
+ Gmail are configured, email it to yourself (the only outbound action the daily
run performs). Replies from contacts we actually emailed transition those outreach
rows ``sent -> replied`` so the lifecycle is tracked in storage. Storage reads and
the reply scan are best-effort: a failure degrades that section to empty rather
than breaking the run.
"""

from __future__ import annotations

from ..digest import render_digest, render_digest_text, send_digest_email, write_digest
from ..logging_config import get_logger
from ..models import Job, Outreach, StageContext, StageResult
from ..outreach.replies import correlate_replies, scan_replies
from ..storage import get_storage
from ..tracker.rows import spreadsheet_url

NAME = "log_and_digest"

log = get_logger(__name__)


def _safe(fn, default, ctx, what):
    """Run a best-effort data-gather; log and fall back to ``default`` on any error."""
    try:
        return fn()
    except Exception as exc:  # observability must not break the digest/run
        log.warning(f"{what} unavailable; skipping", extra={"run_id": ctx.run_id, "error": repr(exc)})
        return default


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    new_jobs: list[Job] = ctx.data.get("new_jobs", [])

    # Pending queues (across all runs) — best-effort. Outreach that became a real
    # Gmail draft is still awaiting the human's send, so it stays in the digest.
    storage = get_storage(s)
    try:
        pending_apps = _safe(
            lambda: storage.list_applications(status="pending_review"), [], ctx, "applications")
        pending_outreach: list[Outreach] = _safe(
            lambda: (
                storage.list_outreach(status="pending_review")
                + storage.list_outreach(status="gmail_draft_created")
            ),
            [], ctx, "outreach",
        )
        replies = _safe(lambda: scan_replies(s), [], ctx, "reply scan")

        # Track the outreach lifecycle: a reply from a contact we emailed moves the
        # row sent -> replied (a storage write, not an outbound action).
        replied_rows = _safe(
            lambda: correlate_replies(replies, storage.list_outreach(status="sent")),
            [], ctx, "reply correlation",
        )
        for outreach in replied_rows:
            outreach.status = "replied"
            _safe(lambda o=outreach: storage.save_outreach(o), None, ctx, "reply status update")
    finally:
        storage.close()

    counts = {
        "new": len(new_jobs),
        "total_sourced": ctx.data.get("jobs_total", 0),
        "applications_prepared": len(ctx.data.get("prepared", [])),
        "applications_pending": len(pending_apps),
        "llm_calls_saved": ctx.data.get("llm_calls_saved", 0),
        "outreach_pending": len(pending_outreach),
        "replies_found": len(replies),
        "replied_matched": len(replied_rows),
    }
    sheet_url = spreadsheet_url(s.sheets_spreadsheet_id) if s.sheets_spreadsheet_id else None

    html = render_digest(
        jobs=new_jobs, run_id=ctx.run_id, counts=counts,
        pending_outreach=pending_outreach, replies=replies, sheet_url=sheet_url,
    )
    path = write_digest(html, s.digest_dir)

    emailed = False
    if s.digest_email_enabled:
        text = render_digest_text(
            jobs=new_jobs, run_id=ctx.run_id, counts=counts,
            pending_outreach=pending_outreach, replies=replies, sheet_url=sheet_url,
        )
        emailed = send_digest_email(html=html, text=text, settings=s)

    log.info(
        "digest written",
        extra={"run_id": ctx.run_id, "stage": NAME, "path": str(path), "emailed": emailed, **counts},
    )
    return StageResult(
        name=NAME,
        counts={"new_jobs_today": len(new_jobs), "digest_written": 1, "digest_emailed": int(emailed),
                "outreach_pending": len(pending_outreach), "applications_pending": len(pending_apps),
                "replies_found": len(replies), "outreach_replied": len(replied_rows)},
        notes=str(path),
    )


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

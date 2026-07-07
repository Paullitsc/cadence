"""Stage: log & digest (Phase 1 file; Phase 4 morning email; Phase 5 outreach focus).

Assemble the daily digest — a compact count header with one link to the Google
Sheet tracker (the application workspace now lives THERE, not in the email),
outreach drafts awaiting approval (including their Gmail-draft links), and possible
recruiter replies — WRITE it to a local HTML file, and, when ``DIGEST_EMAIL_ENABLED``
+ Gmail are configured, email it to yourself (the only outbound action the daily
run performs). Replies from contacts we actually emailed transition those outreach
rows ``sent -> replied``, and applications whose job hasn't been re-seen in
``application_expiry_days`` transition ``pending_review -> expired`` (storage only —
never the tracker sheet's human-owned Status column), so the lifecycle is tracked
in storage and the pending queue doesn't grow unbounded. Storage reads, the expiry
pass, and the reply scan are all best-effort: a failure degrades that section to
empty rather than breaking the run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..digest import render_digest, render_digest_text, send_digest_email, write_digest
from ..logging_config import get_logger
from ..models import Job, Outreach, StageContext, StageResult
from ..outreach.replies import correlate_replies, scan_replies
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


def _expire_stale_applications(storage, settings) -> int:
    """Move ``pending_review`` applications to ``expired`` when their job hasn't
    been re-seen in any feed for ``application_expiry_days`` — the proxy for "this
    posting was filled or pulled". Storage-only: the tracker sheet's Status column
    is human-owned after the initial "prepared" write (see tracker/rows.py) and is
    never touched by this. Disabled when ``application_expiry_days <= 0``.
    """
    if settings.application_expiry_days <= 0:
        return 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=settings.application_expiry_days)
    ).isoformat()
    stale_keys = storage.stale_job_keys(cutoff)
    if not stale_keys:
        return 0
    expired = 0
    for app in storage.list_applications(status="pending_review"):
        if app.dedupe_key in stale_keys:
            app.status = "expired"
            storage.save_application(app)
            expired += 1
    return expired


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    new_jobs: list[Job] = ctx.data.get("new_jobs", [])

    # Pending queues (across all runs) — best-effort. Outreach that became a real
    # Gmail draft is still awaiting the human's send, so it stays in the digest.
    storage = ctx.get_storage()
    # Expire stale applications FIRST so the pending count/queue below already
    # excludes them.
    expired_count = _safe(
        lambda: _expire_stale_applications(storage, s), 0, ctx, "application expiry")
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

    counts = {
        "new": len(new_jobs),
        "total_sourced": ctx.data.get("jobs_total", 0),
        "applications_prepared": len(ctx.data.get("prepared", [])),
        "applications_pending": len(pending_apps),
        "applications_expired": expired_count,
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
                "applications_expired": expired_count,
                "replies_found": len(replies), "outreach_replied": len(replied_rows)},
        notes=str(path),
    )


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

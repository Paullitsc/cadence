"""Stage: log & digest (Phase 1 file; Phase 4 morning-email touchpoint).

Assemble the single daily digest — new jobs, top matches by fit, outreach drafts
awaiting approval, applications prepared awaiting submit, and possible recruiter replies
(a best-effort Gmail scan) — WRITE it to a local HTML file, and, when
``DIGEST_EMAIL_ENABLED`` + Gmail are configured, email it to yourself (the only outbound
action the daily run performs). Storage reads and the reply scan are best-effort: a
failure degrades that section to empty rather than breaking the run. The ``runs`` row is
persisted by the orchestrator once the full run finishes.
"""

from __future__ import annotations

from ..digest import render_digest, render_digest_text, send_digest_email, write_digest
from ..logging_config import get_logger
from ..models import Application, Job, Outreach, StageContext, StageResult
from ..outreach.replies import scan_replies
from ..storage import get_storage

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

    # Pending queues (across all runs, newest/highest-fit first) — best-effort.
    storage = get_storage(s)
    try:
        pending_apps: list[Application] = _safe(
            lambda: storage.list_applications(status="pending_review"), [], ctx, "applications")
        pending_outreach: list[Outreach] = _safe(
            lambda: storage.list_outreach(status="pending_review"), [], ctx, "outreach")
    finally:
        storage.close()

    top_applications = pending_apps[: max(0, s.digest_top_n)]
    replies = _safe(lambda: scan_replies(s), [], ctx, "reply scan")

    counts = {
        "new": len(new_jobs),
        "total_sourced": ctx.data.get("jobs_total", 0),
        "top_matches": len(top_applications),
        "outreach_pending": len(pending_outreach),
        "applications_pending": len(pending_apps),
        "replies_found": len(replies),
    }

    html = render_digest(
        jobs=new_jobs, run_id=ctx.run_id, counts=counts,
        top_applications=top_applications, pending_outreach=pending_outreach,
        pending_applications=pending_apps, replies=replies,
    )
    path = write_digest(html, s.digest_dir)

    emailed = False
    if s.digest_email_enabled:
        text = render_digest_text(
            jobs=new_jobs, run_id=ctx.run_id, counts=counts,
            top_applications=top_applications, pending_outreach=pending_outreach,
            pending_applications=pending_apps, replies=replies,
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
                "replies_found": len(replies)},
        notes=str(path),
    )


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

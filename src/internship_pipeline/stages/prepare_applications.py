"""Stage: prepare applications (Phase 2).

For each application prepared by ``match_and_slice``, draft answers to the standard
application questions using ONLY the candidate's real profile (Claude when
configured; skipped with an empty draft otherwise), and persist them on the
application row. Everything stays ``pending_review`` — the system prepares the
answers; the human edits and submits. Nothing is ever auto-submitted.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import StageContext, StageResult
from ..resume import draft_common_answers, load_master_resume
from ..resume.llm import build_default_complete
from ..storage import get_storage

NAME = "prepare_applications"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    prepared = ctx.data.get("prepared", [])
    if not prepared:
        log.info("no prepared applications to draft answers for", extra={"run_id": ctx.run_id})
        return StageResult(name=NAME, counts={"answers_drafted": 0, "applications_ready": 0})

    resume = ctx.data.get("resume")
    if resume is None:
        try:
            resume = load_master_resume(s.master_resume_file)
        except FileNotFoundError:
            log.warning("master résumé not found; cannot draft answers", extra={"run_id": ctx.run_id})
            return StageResult(name=NAME, counts={"answers_drafted": 0, "applications_ready": 0})

    complete = build_default_complete(s)  # None -> answers skipped (empty drafts)
    if complete is None:
        log.info(
            "no LLM configured; applications stored without drafted answers",
            extra={"run_id": ctx.run_id},
        )

    drafted = 0
    storage = get_storage(s)
    try:
        for item in prepared:
            answers = draft_common_answers(
                job=item.job,
                keywords=item.keywords,
                resume=resume,
                complete=complete,
            )
            item.app.drafted_answers = answers
            item.app.status = "pending_review"  # explicit: never auto-submitted
            storage.save_application(item.app)
            if answers:
                drafted += 1
            log.info(
                "prepared application answers",
                extra={"run_id": ctx.run_id, "company": item.job.company_name,
                       "answers": len(answers)},
            )
    finally:
        storage.close()

    counts = {"answers_drafted": drafted, "applications_ready": len(prepared)}
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"ready={len(prepared)}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

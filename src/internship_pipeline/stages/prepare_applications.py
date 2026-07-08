"""Stage: prepare applications (Phase 2; Phase 5 real ATS form questions).

For each application prepared by ``match_and_slice``, draft answers to the job's
application questions using ONLY the candidate's real profile (Claude when
configured; skipped with an empty draft otherwise), and persist them on the
application row. Only jobs whose ACTUAL free-text form questions are visible
(Greenhouse job-detail API — shape confirmed live; Lever/Ashby expose none) get
an LLM drafting call — there is no generic fallback question set. Select-type
questions (work authorization etc.) are never drafted — they're the human's.
Everything stays ``pending_review`` — the system prepares the answers; the human
edits and submits. Nothing is ever auto-submitted.

Costs: question FETCHES are free public-API calls for every prepared Greenhouse
job; answer drafting is one LLM call per job that has visible questions, capped by
``MAX_QUESTION_DRAFTS_PER_RUN`` (best-fit roles with questions first — the
prepared list is already in fit order).
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import DATA_PREPARED, DATA_RESUME, StageContext, StageResult
from ..resume import draft_common_answers, load_master_resume
from ..resume.llm import build_default_complete
from ..sourcing.http import build_client
from ..sourcing.questions import fetch_greenhouse_questions, greenhouse_ref

NAME = "prepare_applications"

log = get_logger(__name__)


def _real_questions(item, ctx: StageContext, client) -> list[str]:
    """The job's actual free-text form questions, or [] when not fetchable."""
    if client is None:
        return []
    ref = greenhouse_ref(item.job)
    if ref is None:
        return []
    slug, job_id = ref
    try:
        return fetch_greenhouse_questions(
            client, slug=slug, job_id=job_id, max_retries=ctx.settings.http_max_retries
        )
    except Exception as exc:  # skip-on-error: no drafting for this job
        log.warning(
            "question fetch failed; skipping answer drafting",
            extra={"run_id": ctx.run_id, "company": item.job.company_name, "error": repr(exc)},
        )
        return []


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    prepared = ctx.data.get(DATA_PREPARED, [])
    if not prepared:
        log.info("no prepared applications to draft answers for", extra={"run_id": ctx.run_id})
        return StageResult(
            name=NAME,
            counts={"answers_drafted": 0, "applications_ready": 0, "skipped_no_questions": 0},
        )

    resume = ctx.data.get(DATA_RESUME)
    if resume is None:
        try:
            resume = load_master_resume(s.master_resume_file)
        except FileNotFoundError:
            log.warning("master résumé not found; cannot draft answers", extra={"run_id": ctx.run_id})
            return StageResult(
                name=NAME,
                counts={"answers_drafted": 0, "applications_ready": 0, "skipped_no_questions": 0},
            )

    complete = build_default_complete(s)  # None -> answers skipped (empty drafts)
    if complete is None:
        log.info(
            "no LLM configured; applications stored without drafted answers",
            extra={"run_id": ctx.run_id},
        )

    # Real-question fetches are live public-API calls — never made in dry-run, and
    # pointless without an LLM to draft the answers.
    fetch_enabled = complete is not None and not s.dry_run
    client = build_client(s.http_timeout) if fetch_enabled else None

    draft_cap = max(0, s.max_question_drafts_per_run)
    drafted = 0
    real_question_jobs = 0
    skipped_no_questions = 0
    storage = ctx.get_storage()
    try:
        for item in prepared:
            answers: dict[str, str] = {}
            questions: list[str] = []

            if complete is not None:
                questions = _real_questions(item, ctx, client)
                if questions:
                    real_question_jobs += 1
                    if drafted < draft_cap:
                        answers = draft_common_answers(
                            job=item.job,
                            keywords=item.keywords,
                            resume=resume,
                            questions=questions,
                            complete=complete,
                        )
                        if answers:
                            drafted += 1
                    else:
                        log.info(
                            "answer draft cap reached; skipping LLM call",
                            extra={
                                "run_id": ctx.run_id,
                                "company": item.job.company_name,
                                "questions": len(questions),
                            },
                        )
                else:
                    skipped_no_questions += 1

            item.app.drafted_answers = answers
            item.app.status = "pending_review"  # explicit: never auto-submitted
            storage.save_application(item.app)
            log.info(
                "prepared application answers",
                extra={
                    "run_id": ctx.run_id,
                    "company": item.job.company_name,
                    "answers": len(answers),
                    "real_questions": bool(questions),
                },
            )
    finally:
        if client is not None:
            client.close()

    counts = {
        "answers_drafted": drafted,
        "applications_ready": len(prepared),
        "real_question_jobs": real_question_jobs,
        "skipped_no_questions": skipped_no_questions,
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"ready={len(prepared)}")

"""Stage: match & slice (Phase 2).

For each newly-sourced job: embed the JD, score job-to-profile fit and retrieve the
top-K real bullets, extract JD keywords, tailor a one-page résumé from those REAL
bullets only (Claude Haiku when configured; deterministic select-only otherwise),
render a PDF with RenderCV, and store a ``pending_review`` application row.

Reads ``ctx.data["new_jobs"]`` (from the ``source`` stage) and hands the prepared
applications to ``prepare_applications`` via ``ctx.data["prepared"]``. Runs fully
offline with zero credentials (deterministic embedder + select-only tailoring).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logging_config import get_logger
from ..models import Application, Job, StageContext, StageResult
from ..resume import (
    all_bullets,
    build_rendercv_cv,
    get_embedder,
    load_master_resume,
    score_job,
    tailor_resume,
    to_yaml,
)
from ..resume.llm import build_default_complete
from ..resume.matching import job_text
from ..resume.models import BulletRef
from ..resume.rendercv import write_and_render
from ..storage import get_storage
from ..triggers import favorability, is_dual_trigger

NAME = "match_and_slice"

log = get_logger(__name__)


@dataclass
class PreparedApplication:
    """Carries a job + its keywords + the stored application to the next stage.

    ``top_bullets`` are the Phase-2 retrieval result (most relevant real bullets),
    passed through so ``draft_outreach`` can reuse them without re-embedding.
    """

    job: Job
    keywords: list[str]
    app: Application
    top_bullets: list[BulletRef] = field(default_factory=list)
    # Phase 4 dual-trigger: favorable + high-fit → also draft outreach for this role.
    favorable: bool = False
    favorable_reason: str = ""
    dual_trigger: bool = False


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    jobs: list[Job] = ctx.data.get("new_jobs", [])
    if not jobs:
        log.info("no new jobs to score", extra={"run_id": ctx.run_id, "stage": NAME})
        return StageResult(name=NAME, counts={"jobs_scored": 0, "applications_prepared": 0})

    try:
        resume = load_master_resume(s.master_resume_file)
    except FileNotFoundError:
        log.warning(
            "master résumé not found; skipping tailoring (see ACTIONS_FOR_PAUL.md)",
            extra={"run_id": ctx.run_id, "path": s.master_resume_file},
        )
        return StageResult(name=NAME, counts={"jobs_scored": 0, "applications_prepared": 0},
                           notes="no master résumé")

    bullets = all_bullets(resume)
    if not bullets:
        log.warning("master résumé has no bullets; nothing to tailor", extra={"run_id": ctx.run_id})
        return StageResult(name=NAME, counts={"jobs_scored": 0, "applications_prepared": 0})

    embedder = get_embedder(s)
    bullet_vectors = embedder.embed([b.searchable_text() for b in bullets])
    complete = build_default_complete(s)  # None -> deterministic select-only

    # --- Pass 1: score EVERY new job (local embeddings — cheap). ---
    scored = []
    for job in jobs:
        match = score_job(
            job, bullets, bullet_vectors, embedder, resume=resume, top_k=s.top_k_bullets
        )
        if match.fit_score < s.fit_score_threshold:
            log.info(
                "below fit threshold; skipping",
                extra={"run_id": ctx.run_id, "company": job.company_name,
                       "fit": match.fit_score, "threshold": s.fit_score_threshold},
            )
            continue
        scored.append((job, match))

    # --- Cost/volume guard: prepare only the BEST-fit roles this run. Everything
    # above threshold was scored; only the top-N spend LLM calls + a PDF render.
    scored.sort(key=lambda jm: jm[1].fit_score, reverse=True)
    capped = scored[: max(0, s.max_applications_per_run)]
    if len(capped) < len(scored):
        log.info(
            "application cap applied",
            extra={"run_id": ctx.run_id, "above_threshold": len(scored),
                   "prepared_cap": s.max_applications_per_run},
        )

    # --- Pass 2: tailor + render + store the selected roles. ---
    prepared: list[PreparedApplication] = []
    storage = get_storage(s)
    try:
        for job, match in capped:
            fav = favorability(job, s)
            dual = is_dual_trigger(match.fit_score, fav.favorable, s)
            # human_review flags a closer look on high-fit OR target-company roles;
            # the dual-trigger (high-fit AND favorable) is the stricter outreach gate.
            high_priority = (
                match.fit_score >= s.high_priority_threshold
                or job.company_name.lower() in s.target_company_set
            )
            tailored = tailor_resume(
                jd_text=job_text(job),
                keywords=match.keywords,
                candidate_bullets=match.top_bullets,
                resume=resume,
                complete=complete,
                human_review=high_priority,
                max_bullets=s.max_tailored_bullets,
            )
            cv_doc = build_rendercv_cv(resume, tailored.bullets)
            yaml_path, pdf_path = write_and_render(cv_doc, s.resume_output_dir, job.dedupe_key())

            app = Application(
                dedupe_key=job.dedupe_key(),
                company_name=job.company_name,
                title=job.title,
                url=job.url,
                fit_score=match.fit_score,
                keywords=match.keywords,
                tailored_resume_path=pdf_path or yaml_path,  # always the on-disk artifact
                tailored_resume_yaml=to_yaml(cv_doc),
                human_review=tailored.human_review,
                status="pending_review",
            )
            storage.save_application(app)
            prepared.append(
                PreparedApplication(
                    job=job, keywords=match.keywords, app=app, top_bullets=match.top_bullets,
                    favorable=fav.favorable, favorable_reason=fav.reason, dual_trigger=dual,
                )
            )
            log.info(
                "prepared tailored résumé",
                extra={"run_id": ctx.run_id, "company": job.company_name, "title": job.title,
                       "fit": match.fit_score, "human_review": app.human_review,
                       "favorable": fav.favorable, "dual_trigger": dual,
                       "pdf": pdf_path, "used_llm": tailored.used_llm},
            )
    finally:
        storage.close()

    ctx.data["prepared"] = prepared
    ctx.data["resume"] = resume  # reused by prepare_applications (no reload/relog)
    counts = {
        "jobs_scored": len(jobs),
        "above_threshold": len(scored),
        "applications_prepared": len(prepared),
        "high_priority": sum(1 for p in prepared if p.app.human_review),
        "dual_trigger": sum(1 for p in prepared if p.dual_trigger),
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"prepared={len(prepared)}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

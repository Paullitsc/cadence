"""Stage: match & slice (Phase 2; Phase 5 CV grouping + Drive upload).

For each newly-sourced job: embed the JD, score job-to-profile fit and retrieve the
top-K real bullets, extract JD keywords, tailor a one-page résumé from those REAL
bullets only (Claude Haiku when configured; deterministic select-only otherwise),
render a PDF from the Resume.tex-style LaTeX template (trimmed least-relevant-last
until it fits ONE page), and store a ``pending_review`` application row carrying
the AI's recommended bullet selection — the human confirms/adjusts it in the local
review app before the application ever reaches the tracker sheet.

Phase 5 cost saver: before tailoring, the capped job list is CLUSTERED on the JD
embeddings already computed for scoring (cosine ≥ ``CV_GROUP_SIMILARITY``, with a
keyword-overlap sanity check, AND matching ``is_canadian_job`` — see below). One
cluster = one tailoring call + one render + one Drive upload — every other member
reuses the representative's CV. A persistent ``cv_cache`` (keyed by selected-bullet
ids + keyword set + the Canadian flag) additionally reuses CVs across runs.
Rendered PDFs are uploaded to the shared Drive folder when the tracker is
configured — the durable link the sheet shows (local paths die with CI runners).

Each rendered CV's citizenship line is picked by ``is_canadian_job`` (any listed
location reading as Canadian) — Canadian roles get ``MasterResume.citizenship_canada``,
everything else gets ``MasterResume.citizenship``. This is why clustering/caching
gate on it: a US and a Canadian role must never silently share one CV.

Every job is still scored (fit feeds bullet retrieval, sort order, and the
high-priority flag), but ``fit_score_threshold`` only filters ATS/JSearch jobs —
curated-source roles (``github_readme`` README tables, ``simplify`` listings.json,
``landedhq``) skip the gate entirely, since those feeds are already curated to
real, open internships. ``max_applications_per_run`` still caps how many are
prepared per run.

Reads ``ctx.data["new_jobs"]`` (from the ``source`` stage) and hands the prepared
applications to ``prepare_applications`` via ``ctx.data["prepared"]``. Runs fully
offline with zero credentials (deterministic embedder + select-only tailoring +
no-op Drive upload).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..logging_config import get_logger
from ..models import (
    DATA_LLM_CALLS_SAVED,
    DATA_NEW_JOBS,
    DATA_PREPARED,
    DATA_RESUME,
    Application,
    CvCacheEntry,
    Job,
    JobSource,
    StageContext,
    StageResult,
)
from ..resume import (
    all_bullets,
    get_embedder,
    load_master_resume,
    score_job,
    tailor_resume,
    to_yaml,
)
from ..resume.grouping import cluster_jobs, cv_cache_key
from ..resume.llm import build_default_complete
from ..resume.matching import is_canadian_job, job_text
from ..resume.models import BulletRef
from ..resume.render import write_and_render_one_page
from ..tracker.auth import build_tracker_services, tracker_configured
from ..tracker.drive import upload_pdf
from ..triggers import favorability, is_dual_trigger

NAME = "match_and_slice"

# These feeds are curated internship-only lists (GitHub-hosted SimplifyJobs-format
# listings.json + README tables, plus LandedHQ's job tracker) — every row is
# already a real, open internship, so the fit-score gate below is skipped for
# them; it's only there to filter noise out of the ATS/JSearch feeds.
_CURATED_SOURCES = frozenset({JobSource.GITHUB_README, JobSource.SIMPLIFY, JobSource.LANDEDHQ})

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


@dataclass
class _ClusterCv:
    """One tailored CV, shared by every member of a cluster."""

    yaml_text: str
    bullets: list[dict] = field(default_factory=list)  # {"id","text"} recommendation
    pdf_path: Optional[str] = None
    artifact_path: Optional[str] = None  # what Application.tailored_resume_path records
    drive_link: Optional[str] = None
    used_llm: bool = False
    llm_review_flag: bool = False  # the tailoring LLM asked for a closer human look
    from_cache: bool = False


def _identical_cached_cv(
    cache_entries: list[CvCacheEntry], yaml_text: str, *, require_drive_link: bool
) -> Optional[CvCacheEntry]:
    """A cached CV whose YAML is byte-identical to ``yaml_text`` and that still has
    a reusable artifact. With Drive configured, only a twin that already carries a
    Drive link counts (a link-less twin falls through to a fresh render + upload).

    ``cache_entries`` is a run-scoped snapshot (see ``run()``), not a fresh
    ``storage.list_cv_cache()`` call — this scan runs once per cluster, and
    re-fetching the whole table every time would mean one full-table read per
    cluster-with-a-cache-miss, growing with the cache table's size every day.
    """
    for entry in cache_entries:
        if entry.tailored_resume_yaml != yaml_text:
            continue
        if entry.cv_drive_link or (not require_drive_link and entry.pdf_path):
            return entry
    return None


def _cluster_cv(
    job: Job, match, *, resume, complete, settings, storage, drive, cache_entries: list[CvCacheEntry],
    is_canadian: bool = False,
) -> _ClusterCv:
    """Produce the cluster's CV: cache hit → reuse; miss → tailor + render + upload.

    Cache reads/writes are best-effort (a storage hiccup must not stop tailoring).
    A newly-written entry is appended to ``cache_entries`` (the run-scoped
    snapshot) so a LATER cluster in this same run can still content-dedupe
    against it without another round-trip. ``is_canadian`` (the cluster's
    representative job — every member shares the same flag, see ``cluster_jobs``'
    ``group_keys``) picks the rendered citizenship line and is salted into the
    cache key so a US and a Canadian job never reuse each other's CV.
    """
    s = settings
    key = cv_cache_key([b.id for b in match.top_bullets], match.keywords, canadian=is_canadian)
    try:
        entry = storage.get_cv_cache(key)
    except Exception as exc:
        log.warning("cv cache read failed; tailoring fresh", extra={"error": repr(exc)})
        entry = None

    if entry is not None:
        drive_link = entry.cv_drive_link
        # A cached CV from a Drive-less run can gain its durable link now, for free.
        if drive_link is None and drive is not None and entry.pdf_path:
            uploaded = upload_pdf(drive, s.drive_folder_id, entry.pdf_path, f"{key}.pdf")
            if uploaded is not None:
                drive_link = uploaded.web_view_link
                entry.cv_drive_link = drive_link
                entry.drive_file_id = uploaded.file_id
                try:
                    storage.save_cv_cache(entry)
                except Exception as exc:
                    log.warning("cv cache update failed", extra={"error": repr(exc)})
        return _ClusterCv(
            yaml_text=entry.tailored_resume_yaml,
            bullets=entry.recommended_bullets,
            pdf_path=entry.pdf_path,
            artifact_path=entry.pdf_path,
            drive_link=drive_link,
            from_cache=True,
        )

    tailored = tailor_resume(
        jd_text=job_text(job),
        keywords=match.keywords,
        candidate_bullets=match.top_bullets,
        resume=resume,
        complete=complete,
        human_review=False,  # per-job priority is applied per member, not baked in here
        max_bullets=s.max_tailored_bullets,
    )
    # Render the Resume.tex-style PDF, trimming least-relevant-last until it fits
    # one page — the YAML/recommendation stored below reflect the TRIMMED page.
    render = write_and_render_one_page(
        resume, tailored.bullets, s.resume_output_dir, job.dedupe_key(), is_canadian=is_canadian
    )
    yaml_text = to_yaml(render.cv_doc)
    recommendation = [{"id": tb.ref.id, "text": tb.text} for tb in render.bullets]

    # Content dedupe: different cache keys (e.g. different JD keyword sets) can still
    # tailor to a byte-identical CV. Reuse the twin's Drive link instead of minting a
    # duplicate Drive file — the sheet then shows "same as row N", not a second link
    # to the same document. (The LLM call and local render already happened; this
    # saves the upload and keeps one durable artifact per unique CV.)
    twin = _identical_cached_cv(cache_entries, yaml_text, require_drive_link=drive is not None)
    if twin is not None:
        log.info(
            "identical CV content; reusing existing artifact",
            extra={"company": job.company_name, "twin_key": twin.cache_key},
        )
        new_entry = CvCacheEntry(
            cache_key=key,
            tailored_resume_yaml=yaml_text,
            cv_drive_link=twin.cv_drive_link,
            drive_file_id=twin.drive_file_id,
            pdf_path=render.pdf_path or twin.pdf_path,
            recommended_bullets=recommendation,
        )
        try:
            storage.save_cv_cache(new_entry)
            cache_entries.append(new_entry)
        except Exception as exc:
            log.warning("cv cache write failed", extra={"error": repr(exc)})
        return _ClusterCv(
            yaml_text=yaml_text,
            bullets=recommendation,
            pdf_path=render.pdf_path or twin.pdf_path,
            artifact_path=render.pdf_path or twin.pdf_path,
            drive_link=twin.cv_drive_link,
            used_llm=tailored.used_llm,
            llm_review_flag=tailored.human_review,
        )

    drive_link = None
    drive_file_id = None
    if drive is not None and render.pdf_path:
        # Named after the CV's cache key (its content identity: bullets +
        # keywords), not the representative job's dedupe key — matches the
        # cache-backfill upload above and stays stable across runs/clusters
        # that reuse this same tailored CV for a different representative job.
        uploaded = upload_pdf(drive, s.drive_folder_id, render.pdf_path, f"{key}.pdf")
        if uploaded is not None:
            drive_link, drive_file_id = uploaded.web_view_link, uploaded.file_id

    new_entry = CvCacheEntry(
        cache_key=key,
        tailored_resume_yaml=yaml_text,
        cv_drive_link=drive_link,
        drive_file_id=drive_file_id,
        pdf_path=render.pdf_path,
        recommended_bullets=recommendation,
    )
    try:
        storage.save_cv_cache(new_entry)
        cache_entries.append(new_entry)
    except Exception as exc:
        log.warning("cv cache write failed", extra={"error": repr(exc)})

    return _ClusterCv(
        yaml_text=yaml_text,
        bullets=recommendation,
        pdf_path=render.pdf_path,
        artifact_path=render.pdf_path or render.yaml_path,  # always the on-disk artifact
        drive_link=drive_link,
        used_llm=tailored.used_llm,
        llm_review_flag=tailored.human_review,
    )


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    jobs: list[Job] = ctx.data.get(DATA_NEW_JOBS, [])
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
        if job.source_feed not in _CURATED_SOURCES and match.fit_score < s.fit_score_threshold:
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

    # --- Phase 5: cluster the capped list so similar JDs share ONE tailored CV. ---
    # The vectors were computed for scoring — clustering costs nothing extra.
    # group_keys=canadian_flags keeps a Canadian and a non-Canadian role from
    # sharing a CV even with a near-identical JD (the citizenship line differs).
    canadian_flags = [is_canadian_job(job) for job, _ in capped]
    clusters = cluster_jobs(
        [m.jd_vector for _, m in capped],
        [m.keywords for _, m in capped],
        similarity_threshold=s.cv_group_similarity,
        keyword_overlap_threshold=s.cv_group_keyword_overlap,
        group_keys=canadian_flags,
    )
    if len(clusters) < len(capped):
        log.info(
            "cv grouping collapsed similar roles",
            extra={"run_id": ctx.run_id, "roles": len(capped), "clusters": len(clusters)},
        )

    # Drive upload is quietly skipped unless the tracker + folder are configured.
    drive = None
    if tracker_configured(s) and s.drive_folder_id:
        services = build_tracker_services(s)
        drive = services.drive if services else None

    # --- Pass 2: tailor + render + upload ONCE per cluster, then store every member. ---
    prepared: list[PreparedApplication] = []
    cache_hits = 0
    cv_by_index: dict[int, _ClusterCv] = {}
    storage = ctx.get_storage()
    # One snapshot for the whole run, not one storage.list_cv_cache() per cluster
    # cache-miss — _cluster_cv appends its own new entries so later clusters in
    # this run still content-dedupe correctly against them.
    try:
        cache_entries = storage.list_cv_cache()
    except Exception as exc:
        log.warning("cv cache list failed; content-dedupe scan skipped", extra={"error": repr(exc)})
        cache_entries = []
    for cluster in clusters:
        rep_job, rep_match = capped[cluster.representative]
        cv = _cluster_cv(rep_job, rep_match, resume=resume, complete=complete,
                         settings=s, storage=storage, drive=drive, cache_entries=cache_entries,
                         is_canadian=canadian_flags[cluster.representative])
        if cv.from_cache:
            cache_hits += 1
        for idx in cluster.members:
            cv_by_index[idx] = cv

    for idx, (job, match) in enumerate(capped):
        cv = cv_by_index[idx]
        fav = favorability(job, s)
        dual = is_dual_trigger(match.fit_score, fav.favorable, s)
        # human_review flags a closer look on high-fit OR target-company roles;
        # the dual-trigger (high-fit AND favorable) is the stricter outreach gate.
        high_priority = (
            match.fit_score >= s.high_priority_threshold
            or job.company_name.lower() in s.target_company_set
        )
        app = Application(
            dedupe_key=job.dedupe_key(),
            company_name=job.company_name,
            title=job.title,
            url=job.url,
            fit_score=match.fit_score,
            keywords=match.keywords,
            tailored_resume_path=cv.artifact_path,
            tailored_resume_yaml=cv.yaml_text,
            cv_drive_link=cv.drive_link,
            recommended_bullets=cv.bullets,
            human_review=high_priority or cv.llm_review_flag,
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
                   "pdf": cv.pdf_path, "drive_link": cv.drive_link,
                   "used_llm": cv.used_llm, "cv_from_cache": cv.from_cache},
        )

    # LLM calls saved = within-run cluster reuse + cross-run cache hits. Only counted
    # when an LLM is actually configured (deterministic tailoring is free anyway).
    reused_in_run = len(capped) - len(clusters)
    llm_calls_saved = (reused_in_run + cache_hits) if complete is not None else 0
    if reused_in_run or cache_hits:
        log.info(
            "cv reuse saved tailoring work",
            extra={"run_id": ctx.run_id, "reused_in_run": reused_in_run,
                   "cache_hits": cache_hits, "llm_calls_saved": llm_calls_saved},
        )

    ctx.data[DATA_PREPARED] = prepared
    ctx.data[DATA_RESUME] = resume  # reused by prepare_applications (no reload/relog)
    ctx.data[DATA_LLM_CALLS_SAVED] = llm_calls_saved  # shown in the digest header
    counts = {
        "jobs_scored": len(jobs),
        "above_threshold": len(scored),
        "applications_prepared": len(prepared),
        "high_priority": sum(1 for p in prepared if p.app.human_review),
        "dual_trigger": sum(1 for p in prepared if p.dual_trigger),
        "cv_clusters": len(clusters),
        "cv_reused_in_run": reused_in_run,
        "cv_cache_hits": cache_hits,
        "llm_calls_saved": llm_calls_saved,
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"prepared={len(prepared)}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

"""Offline stage-level test for Phase 2 (no network, no API key, no rendercv).

Exercises match_and_slice -> prepare_applications end-to-end with the deterministic
hashing embedder and no LLM, asserting a pending_review application is persisted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.models import Job, StageContext
from internship_pipeline.stages import match_and_slice, prepare_applications
from internship_pipeline.storage import get_storage

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


@pytest.fixture
def phase2_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        master_resume_file=FIXTURE,
        embedding_backend="hashing",   # deterministic, offline
        resume_output_dir=str(tmp_path / "resumes"),
        fit_score_threshold=0.0,       # prepare regardless of hashing similarity
        high_priority_threshold=1.1,   # nothing auto-flagged high priority
        anthropic_api_key=None,        # no LLM -> deterministic tailoring, no answers
    )


def test_match_and_prepare_persist_pending_review_application(phase2_settings):
    job = Job(
        company_name="DataCorp",
        title="Backend Engineering Intern",
        url="https://x/backend-intern",
        description="Build data pipelines in Python and Kafka on the backend team.",
    )
    ctx = StageContext(run_id="t1", settings=phase2_settings)
    ctx.data["new_jobs"] = [job]

    match_result = match_and_slice.run(ctx)
    assert match_result.counts["jobs_scored"] == 1
    assert match_result.counts["applications_prepared"] == 1
    # a résumé artifact (YAML, since rendercv isn't installed) was written to disk
    prepared = ctx.data["prepared"]
    assert len(prepared) == 1
    assert Path(prepared[0].app.tailored_resume_path).exists()

    prep_result = prepare_applications.run(ctx)
    assert prep_result.counts["applications_ready"] == 1
    assert prep_result.counts["answers_drafted"] == 0  # no LLM configured

    store = get_storage(phase2_settings)
    try:
        app = store.get_application(job.dedupe_key())
    finally:
        store.close()
    assert app is not None
    assert app.status == "pending_review"
    assert app.fit_score > 0.0
    assert app.tailored_resume_yaml and "Test Candidate" in app.tailored_resume_yaml
    assert app.drafted_answers == {}


def test_match_and_slice_noop_without_new_jobs(phase2_settings):
    ctx = StageContext(run_id="t2", settings=phase2_settings)
    result = match_and_slice.run(ctx)
    assert result.counts == {"jobs_scored": 0, "applications_prepared": 0}


class _CvCacheStub:
    """Duck-typed stand-in for the three cache methods ``_cluster_cv`` uses."""

    def __init__(self, entries):
        self.entries = entries
        self.saved = []

    def get_cv_cache(self, cache_key):
        return None  # force a key miss so the content-dedupe scan runs

    def list_cv_cache(self):
        return self.entries

    def save_cv_cache(self, entry):
        self.saved.append(entry)


def test_identical_cv_content_reuses_twin_artifact(phase2_settings, monkeypatch):
    """A byte-identical CV cached under a DIFFERENT key (different keywords) must
    reuse the twin's Drive link/PDF instead of rendering + uploading a duplicate."""
    from internship_pipeline.models import CvCacheEntry
    from internship_pipeline.resume import (
        all_bullets,
        build_rendercv_cv,
        get_embedder,
        load_master_resume,
        score_job,
        to_yaml,
    )
    from internship_pipeline.resume.matching import job_text
    from internship_pipeline.resume.tailoring import tailor_resume

    s = phase2_settings
    resume = load_master_resume(s.master_resume_file)
    bullets = all_bullets(resume)
    embedder = get_embedder(s)
    vectors = embedder.embed([b.searchable_text() for b in bullets])
    job = Job(company_name="DataCorp", title="Backend Intern", url="https://x/twin",
              description="Build data pipelines in Python and Kafka on the backend team.")
    match = score_job(job, bullets, vectors, embedder, resume=resume, top_k=s.top_k_bullets)

    # Pre-compute the exact YAML _cluster_cv will produce (deterministic, no LLM)
    # and seed the cache with it under an unrelated key.
    tailored = tailor_resume(
        jd_text=job_text(job), keywords=match.keywords, candidate_bullets=match.top_bullets,
        resume=resume, complete=None, human_review=False, max_bullets=s.max_tailored_bullets,
    )
    twin_yaml = to_yaml(build_rendercv_cv(resume, tailored.bullets))
    twin = CvCacheEntry(cache_key="other-key", tailored_resume_yaml=twin_yaml,
                        cv_drive_link="https://drive/twin", pdf_path="/old/twin.pdf")
    storage = _CvCacheStub([twin])

    def _no_render(*a, **k):
        raise AssertionError("identical CV must not be re-rendered")

    monkeypatch.setattr(match_and_slice, "write_and_render", _no_render)
    cv = match_and_slice._cluster_cv(
        job, match, resume=resume, complete=None, settings=s, storage=storage, drive=None,
        cache_entries=storage.entries,
    )
    assert cv.drive_link == "https://drive/twin"
    assert cv.pdf_path == "/old/twin.pdf"
    assert cv.from_cache is False  # the LLM/tailoring DID run; only render+upload saved
    # The twin's artifacts were re-cached under this job's own key.
    assert storage.saved and storage.saved[0].cv_drive_link == "https://drive/twin"


def test_twin_without_drive_link_not_reused_when_drive_configured():
    """With Drive on, a link-less twin must fall through to a fresh render+upload."""
    from internship_pipeline.models import CvCacheEntry

    linkless = CvCacheEntry(cache_key="k", tailored_resume_yaml="cv: x", pdf_path="/x.pdf")
    entries = [linkless]
    assert match_and_slice._identical_cached_cv(entries, "cv: x", require_drive_link=True) is None
    hit = match_and_slice._identical_cached_cv(entries, "cv: x", require_drive_link=False)
    assert hit is linkless


def test_cv_cache_listed_once_per_run_not_once_per_cluster(phase2_settings, monkeypatch):
    """Multiple cache-miss clusters in one run must share a single
    storage.list_cv_cache() snapshot, not each re-fetch the whole table."""
    from internship_pipeline.storage.sqlite_store import SQLiteStore

    calls = []
    original = SQLiteStore.list_cv_cache

    def counting_list_cv_cache(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(SQLiteStore, "list_cv_cache", counting_list_cv_cache)

    # Three jobs with unrelated descriptions -> three distinct clusters (no
    # grouping), each a cache miss on a fresh DB.
    jobs = [
        Job(company_name=f"Co{i}", title="Intern", url=f"https://x/{i}", description=desc)
        for i, desc in enumerate([
            "Build data pipelines in Python and Kafka on the backend team.",
            "Barista role preparing espresso drinks at a cafe.",
            "Groundskeeping and landscaping position outdoors.",
        ])
    ]
    ctx = StageContext(run_id="t4", settings=phase2_settings)
    ctx.data["new_jobs"] = jobs

    result = match_and_slice.run(ctx)
    assert result.counts["applications_prepared"] == 3
    assert len(calls) == 1  # one snapshot for the whole run, not one per cluster


def test_application_cap_prepares_only_best_fit(phase2_settings):
    """Cost guard: all jobs are scored, but only the top-N by fit are prepared."""
    phase2_settings = phase2_settings.model_copy(update={"max_applications_per_run": 1})
    jobs = [
        Job(company_name=f"Co{i}", title="Backend Intern", url=f"https://x/{i}",
            description=desc)
        for i, desc in enumerate([
            "Build data pipelines in Python and Kafka on the backend team.",  # best match
            "Barista role preparing espresso drinks.",                        # weak match
            "Groundskeeping and landscaping position.",                       # weak match
        ])
    ]
    ctx = StageContext(run_id="t3", settings=phase2_settings)
    ctx.data["new_jobs"] = jobs

    result = match_and_slice.run(ctx)
    assert result.counts["jobs_scored"] == 3
    assert result.counts["applications_prepared"] == 1  # capped
    prepared = ctx.data["prepared"]
    # the ONE prepared role is the best-fit one, not merely the first encountered
    best = max(prepared, key=lambda p: p.app.fit_score)
    assert prepared[0].app.fit_score == best.app.fit_score
    assert result.counts["above_threshold"] >= 1

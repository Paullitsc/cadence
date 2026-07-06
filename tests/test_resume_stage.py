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

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

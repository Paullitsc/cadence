"""prepare_applications: draft only when real ATS questions are visible."""

from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.models import Application, Job, JobSource, StageContext
from internship_pipeline.resume.loader import load_master_resume
from internship_pipeline.stages import prepare_applications
from internship_pipeline.stages.match_and_slice import PreparedApplication
from internship_pipeline.storage import get_storage

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


def _gh_job(company: str, job_id: str, *, url: str | None = None) -> Job:
    return Job(
        company_name=company,
        title="SWE Intern",
        url=url or f"https://boards.greenhouse.io/{company.lower()}/jobs/{job_id}",
        source=f"greenhouse:{company.lower()}",
        source_feed=JobSource.GREENHOUSE,
    )


def _prepared(job: Job, *, fit: float = 0.6) -> PreparedApplication:
    return PreparedApplication(
        job=job,
        keywords=["python"],
        app=Application(
            dedupe_key=job.dedupe_key(),
            company_name=job.company_name,
            title=job.title,
            url=job.url,
            fit_score=fit,
        ),
        top_bullets=[],
    )


@pytest.fixture
def prep_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        master_resume_file=FIXTURE,
        anthropic_api_key="test-key",  # forces complete != None path
        dry_run=False,
        max_question_drafts_per_run=2,
    )


def test_skips_llm_when_no_questions_visible(prep_settings, monkeypatch):
    """Non-Greenhouse (or empty fetch) jobs must not call the LLM at all."""
    calls: list[tuple] = []

    def boom(system_blocks, user_text):
        calls.append((system_blocks, user_text))
        raise AssertionError("LLM must not be called without real questions")

    monkeypatch.setattr(
        prepare_applications,
        "build_default_complete",
        lambda s: boom,
    )
    monkeypatch.setattr(prepare_applications, "_real_questions", lambda *a, **k: [])

    ctx = StageContext(run_id="t-skip", settings=prep_settings)
    ctx.data["prepared"] = [
        _prepared(Job(company_name="LeverCo", title="Intern", url="https://jobs.lever.co/x/1")),
        _prepared(_gh_job("Stripe", "111")),
    ]
    ctx.data["resume"] = load_master_resume(FIXTURE)

    result = prepare_applications.run(ctx)
    assert calls == []
    assert result.counts["answers_drafted"] == 0
    assert result.counts["skipped_no_questions"] == 2


def test_drafts_when_real_questions_fetched(prep_settings, monkeypatch):
    questions = ["Why do you want to work here?"]

    def fake_complete(system_blocks, user_text):
        return {"answers": {questions[0]: "Because backend pipelines."}}

    monkeypatch.setattr(
        prepare_applications,
        "build_default_complete",
        lambda s: fake_complete,
    )
    monkeypatch.setattr(prepare_applications, "_real_questions", lambda *a, **k: questions)

    job = _gh_job("Stripe", "7954688")
    ctx = StageContext(run_id="t-draft", settings=prep_settings)
    ctx.data["prepared"] = [_prepared(job)]
    ctx.data["resume"] = load_master_resume(FIXTURE)

    result = prepare_applications.run(ctx)
    assert result.counts["answers_drafted"] == 1
    assert result.counts["real_question_jobs"] == 1
    assert result.counts["skipped_no_questions"] == 0

    store = get_storage(prep_settings)
    try:
        app = store.get_application(job.dedupe_key())
    finally:
        store.close()
    assert app.drafted_answers == {questions[0]: "Because backend pipelines."}


def test_draft_cap_counts_llm_calls_not_list_positions(prep_settings, monkeypatch):
    """Cap bounds actual drafts: jobs 1-2 draft, job 3 has questions but is over cap."""
    drafted_jobs: list[str] = []

    def fake_complete(system_blocks, user_text):
        # user_text: "JOB: {title} at {company_name}"
        company = user_text.split(" at ", 1)[1].split("\n", 1)[0].strip()
        drafted_jobs.append(company)
        return {"answers": {"Why?": "Because."}}

    monkeypatch.setattr(
        prepare_applications,
        "build_default_complete",
        lambda s: fake_complete,
    )

    def fake_questions(item, ctx, client):
        return ["Why?"] if item.job.company_name.startswith("HasQ") else []

    monkeypatch.setattr(prepare_applications, "_real_questions", fake_questions)

    ctx = StageContext(run_id="t-cap", settings=prep_settings)
    ctx.data["prepared"] = [
        _prepared(Job(company_name="NoQ", title="Intern", url="https://x.dev/1")),
        _prepared(_gh_job("HasQ1", "1")),
        _prepared(_gh_job("HasQ2", "2")),
        _prepared(_gh_job("HasQ3", "3")),
    ]
    ctx.data["resume"] = load_master_resume(FIXTURE)

    result = prepare_applications.run(ctx)
    assert result.counts["answers_drafted"] == 2
    assert result.counts["real_question_jobs"] == 3
    assert result.counts["skipped_no_questions"] == 1
    assert drafted_jobs == ["HasQ1", "HasQ2"]

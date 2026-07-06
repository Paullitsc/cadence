"""Phase 5: real Greenhouse form questions — pure parsing from a JSON fixture."""

from __future__ import annotations

import json
from pathlib import Path

from internship_pipeline.models import Job, JobSource
from internship_pipeline.sourcing.questions import (
    greenhouse_ref,
    parse_greenhouse_job_url,
    parse_greenhouse_questions,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict:
    return json.loads((FIXTURES / "greenhouse_job_questions.json").read_text(encoding="utf-8"))


def test_parse_keeps_only_custom_free_text_questions():
    questions = parse_greenhouse_questions(_payload())
    assert questions == [
        "Who is your current or previous employer?",
        "Why do you want to work here?",  # whitespace normalized
    ]


def test_parse_drops_identity_file_and_select_questions():
    questions = parse_greenhouse_questions(_payload())
    assert "First Name" not in questions
    assert "Email" not in questions
    assert all("Resume" not in q and "Cover Letter" not in q for q in questions)
    # Selects (work auth, degree) are the human's to click — never drafted.
    assert all("authorized" not in q.lower() for q in questions)
    assert all("degree" not in q.lower() for q in questions)


def test_parse_tolerates_garbage():
    assert parse_greenhouse_questions({}) == []
    assert parse_greenhouse_questions({"questions": None}) == []
    assert parse_greenhouse_questions({"questions": ["nope", {"label": ""}]}) == []


def test_parse_job_url_hosted_board():
    slug, job_id = parse_greenhouse_job_url("https://boards.greenhouse.io/stripe/jobs/7954688")
    assert (slug, job_id) == ("stripe", "7954688")
    slug, job_id = parse_greenhouse_job_url(
        "https://job-boards.greenhouse.io/ramp/jobs/123456/"
    )
    assert (slug, job_id) == ("ramp", "123456")


def test_parse_job_url_company_domain_gh_jid():
    slug, job_id = parse_greenhouse_job_url("https://stripe.com/jobs/search?gh_jid=7954688")
    assert slug is None  # company domain carries no board token
    assert job_id == "7954688"


def test_greenhouse_ref_prefers_source_tag_for_slug():
    job = Job(
        company_name="Stripe",
        title="SWE Intern",
        url="https://stripe.com/jobs/search?gh_jid=7954688",
        source="greenhouse:stripe",
        source_feed=JobSource.GREENHOUSE,
    )
    assert greenhouse_ref(job) == ("stripe", "7954688")


def test_greenhouse_ref_none_when_unresolvable():
    job = Job(company_name="X", title="Intern", url="https://x.dev/careers/intern")
    assert greenhouse_ref(job) is None

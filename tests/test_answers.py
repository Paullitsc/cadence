"""Application answer drafting: empty questions skipped, empty answers dropped."""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.models import Job
from internship_pipeline.resume.answers import draft_common_answers
from internship_pipeline.resume.loader import load_master_resume

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")
JOB = Job(company_name="Acme", title="Backend Intern", url="https://acme.com/1",
          description="Build data pipelines in Python.")


def test_empty_questions_returns_empty_without_llm_call():
    calls = 0

    def boom(system_blocks, user_text):
        nonlocal calls
        calls += 1
        return {}

    resume = load_master_resume(FIXTURE)
    out = draft_common_answers(
        job=JOB, keywords=["python"], resume=resume,
        questions=[], complete=boom,
    )
    assert out == {}
    assert calls == 0


def test_empty_string_answers_are_dropped():
    questions = ["Why us?", "Salary expectations?"]

    def fake_complete(system_blocks, user_text):
        return {
            "answers": {
                "Why us?": "The backend work matches my pipeline project.",
                "Salary expectations?": "",
            }
        }

    resume = load_master_resume(FIXTURE)
    out = draft_common_answers(
        job=JOB, keywords=["python"], resume=resume,
        questions=questions, complete=fake_complete,
    )
    assert out == {"Why us?": "The backend work matches my pipeline project."}

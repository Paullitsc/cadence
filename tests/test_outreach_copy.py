"""Outreach copy: deterministic templates are real + grounded, and no fabricated
LLM fact can reach a drafted message (same anti-hallucination guarantee as Phase 2).
"""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.models import Job
from internship_pipeline.outreach.contacts import Contact
from internship_pipeline.outreach.copy import (
    LINKEDIN_NOTE_LIMIT,
    allowed_vocab,
    deterministic_linkedin,
    draft_outreach_copy,
    is_grounded,
)
from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.matching import content_tokens

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")

JOB = Job(company_name="Acme Labs", title="Backend Intern",
          url="https://acme.com/jobs/1", description="Build data pipelines in Python and Kafka.")
KEYWORDS = ["python", "kafka", "pipeline"]
CONTACT = Contact(name="Ada Lovelace", title="Recruiter", source="hunter", verified=True)


def _setup():
    resume = load_master_resume(FIXTURE)
    return resume, all_bullets(resume)


def test_is_grounded_gate():
    vocab = {"built", "python", "pipeline"}
    assert is_grounded("Built Python pipeline", vocab) is True
    assert is_grounded("Built Rust pipeline", vocab) is False


def test_deterministic_copy_is_real_and_references_company_and_bullets():
    resume, bullets = _setup()
    content = draft_outreach_copy(job=JOB, contact=CONTACT, keywords=KEYWORDS,
                                  top_bullets=bullets, resume=resume, complete=None)
    assert content.used_llm is False
    assert "Acme Labs" in content.email_body and "Backend Intern" in content.email_body
    assert "Ada" in content.email_body  # greets the real contact by first name
    assert bullets[0].text in content.email_body  # a real bullet, verbatim
    assert content.subject and "Acme Labs" in content.subject


def test_llm_fabrication_falls_back_to_deterministic_per_field():
    resume, bullets = _setup()

    def fake_complete(system_blocks, user_text):
        # Tries to fabricate an employer, a metric, and an un-cited technology.
        return {
            "subject": "Interested in the Backend Intern role at Acme Labs",  # grounded → kept
            "email_body": "Hi Ada, I boosted revenue 400% at Google using Rust.",  # ungrounded
            "linkedin_note": "I scaled Google to 400% growth with Rust.",  # ungrounded
        }

    content = draft_outreach_copy(job=JOB, contact=CONTACT, keywords=KEYWORDS,
                                  top_bullets=bullets, resume=resume, complete=fake_complete)
    assert content.used_llm is True
    # The grounded subject survived...
    assert content.subject == "Interested in the Backend Intern role at Acme Labs"
    # ...but the fabricated fields fell back to the deterministic, grounded template.
    body_tokens = content_tokens(content.email_body) | content_tokens(content.linkedin_note)
    for forbidden in ("google", "rust", "revenue", "400"):
        assert forbidden not in body_tokens


def test_llm_failure_is_swallowed_into_deterministic_copy():
    resume, bullets = _setup()

    def boom(system_blocks, user_text):
        raise RuntimeError("api down")

    content = draft_outreach_copy(job=JOB, contact=CONTACT, keywords=KEYWORDS,
                                  top_bullets=bullets, resume=resume, complete=boom)
    assert content.used_llm is False  # error → safe template, run not broken
    assert "Acme Labs" in content.email_body


def test_grounded_gate_accepts_the_deterministic_email_body_generatively():
    """The allowed vocab must be wide enough that an honest draft is not rejected."""
    resume, bullets = _setup()
    vocab = allowed_vocab(JOB, KEYWORDS, bullets, resume, CONTACT)
    # A plausible honest sentence built only from job + real-bullet + boilerplate words.
    assert is_grounded("Hi Ada, I built a Python data pipeline with Kafka.", vocab) is True


def test_linkedin_note_respects_length_cap():
    resume, bullets = _setup()
    long_job = Job(company_name="A" * 400, title="Backend Intern", url="https://a.com/1")
    note = deterministic_linkedin(long_job, resume, bullets, CONTACT)
    assert len(note) <= LINKEDIN_NOTE_LIMIT and note.endswith("…")

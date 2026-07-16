"""Networking copy: deterministic templates are real, the connect note respects
LinkedIn's length cap, and no fabricated LLM fact can reach a draft."""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.networking.copy import (
    deterministic_connect_note,
    deterministic_message,
    draft_networking_copy,
    networking_vocab,
    rank_bullets,
)
from internship_pipeline.networking.models import Person
from internship_pipeline.outreach.copy import LINKEDIN_NOTE_LIMIT
from internship_pipeline.resume.loader import all_bullets, load_master_resume

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")

PERSON = Person(
    person_id="test-robotics-co-1",
    company_name="Robotics Co",
    company_blurb="Robotics Co builds data infrastructure for warehouse robots in Python.",
    name="Jane Doe",
    role="CTO",
)


def _setup():
    resume = load_master_resume(FIXTURE)
    return resume, all_bullets(resume)


def test_rank_bullets_prefers_blurb_overlap():
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, PERSON, limit=2)
    # The Python/data-pipeline bullet overlaps the blurb ("data", "python");
    # the Java query-planner one doesn't.
    assert "data pipeline" in top[0].text


def test_deterministic_connect_note_is_real_and_short():
    resume, _ = _setup()
    note = deterministic_connect_note(PERSON, resume)
    assert len(note) <= LINKEDIN_NOTE_LIMIT
    assert "Robotics Co" in note
    assert note.startswith("Hi Jane")
    assert "Test" in note  # the candidate's real first name


def test_deterministic_message_references_company_and_real_bullets():
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, PERSON, limit=2)
    message = deterministic_message(PERSON, resume, top)
    assert "Robotics Co" in message
    assert top[0].text.replace("**", "") in message
    assert "Test Candidate" in message  # signs with the real name


def test_no_llm_returns_deterministic():
    resume, bullets = _setup()
    note, message = draft_networking_copy(
        person=PERSON, resume=resume, top_bullets=bullets[:2], complete=None
    )
    assert note.used_llm is False and message.used_llm is False
    assert note.body == deterministic_connect_note(PERSON, resume)


def test_llm_fabrication_falls_back_per_field():
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, PERSON, limit=2)

    def fake_complete(system_blocks, user_text):
        return {
            # Grounded (company + real bullet vocabulary) → kept.
            "connect_note": "Hi Jane — I built a data pipeline in Python and "
                            "would love to connect and follow Robotics Co's work!",
            # Fabricated employer/metric/tech → rejected, deterministic used.
            "message": "Hi Jane, I boosted revenue 400% at Google using Rust.",
        }

    note, message = draft_networking_copy(
        person=PERSON, resume=resume, top_bullets=top, complete=fake_complete
    )
    assert note.used_llm is True
    assert "data pipeline" in note.body
    assert message.used_llm is False
    for token in ("Google", "Rust", "400"):
        assert token not in message.body


def test_llm_overlong_connect_note_is_rejected():
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, PERSON, limit=2)
    vocab_filler = "connect " * 80  # grounded but way past the 300-char cap

    def fake_complete(system_blocks, user_text):
        return {"connect_note": f"Hi Jane — {vocab_filler}", "message": ""}

    note, message = draft_networking_copy(
        person=PERSON, resume=resume, top_bullets=top, complete=fake_complete
    )
    assert note.used_llm is False
    assert len(note.body) <= LINKEDIN_NOTE_LIMIT
    assert message.used_llm is False  # empty field → deterministic


def test_llm_error_falls_back_entirely():
    resume, bullets = _setup()

    def boom(system_blocks, user_text):
        raise RuntimeError("api down")

    note, message = draft_networking_copy(
        person=PERSON, resume=resume, top_bullets=bullets[:2], complete=boom
    )
    assert note.used_llm is False and message.used_llm is False
    assert note.body and message.body


def test_vocab_includes_blurb_and_bullets_only_from_real_sources():
    resume, bullets = _setup()
    vocab = networking_vocab(PERSON, resume, bullets[:2])
    assert "warehouse" in vocab  # from the blurb
    assert "kafka" in vocab  # from a real bullet
    assert "google" not in vocab

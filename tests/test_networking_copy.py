"""Networking copy: deterministic templates are real, the connect note respects
LinkedIn's length cap, and no fabricated LLM fact can reach a draft."""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.networking.copy import (
    _build_user_text,
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

# The same person once the roster carries the hand-researched personalization.
HOOK = "their scheduler reassigning work mid-shift when a machine drops out"
BACKGROUND = "building the onboard perception stack"
RESEARCHED = PERSON.model_copy(update={"company_hook": HOOK, "background": BACKGROUND})


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
    # The bullet keeps its own wording; only its trailing period is dropped so the
    # source parenthetical can follow it.
    assert top[0].text.replace("**", "").rstrip(".") in message
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


# --------------------------------------------------------------------------- #
# Personalization: the researched hook + background are what make a draft land
# --------------------------------------------------------------------------- #
def test_message_names_the_hook_and_asks_about_them():
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, RESEARCHED, limit=2)
    message = deterministic_message(RESEARCHED, resume, top)
    # The two specific beats, and a question aimed at the recipient.
    assert HOOK in message
    assert BACKGROUND in message
    assert "15 minutes" in message
    # The failure mode this rewrite exists to kill: a bullet dump plus an
    # internship ask, which reads as a form letter.
    assert "\n- " not in message
    assert "intern" not in message.lower()
    assert top[0].text.replace("**", "") not in message


def test_message_degrades_to_one_credential_without_research():
    """No hook/background yet → still prose, still a call ask, never a list."""
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, PERSON, limit=2)
    message = deterministic_message(PERSON, resume, top)
    assert top[0].text.replace("**", "").rstrip(".") in message  # one, not two
    assert top[1].text.replace("**", "") not in message
    assert "\n- " not in message
    assert "intern" not in message.lower()


def test_connect_note_names_the_company_before_the_hook():
    """Hooks are noun phrases that often open with a possessive, so the company
    has to be introduced first or 'their' dangles."""
    resume, _ = _setup()
    note = deterministic_connect_note(RESEARCHED, resume)
    assert HOOK in note
    assert len(note) <= LINKEDIN_NOTE_LIMIT
    assert note.index("Robotics Co") < note.index(HOOK)


def test_overlong_hook_falls_back_to_the_generic_note():
    resume, _ = _setup()
    verbose = PERSON.model_copy(update={"company_hook": "a scheduler " * 40})
    note = deterministic_connect_note(verbose, resume)
    assert len(note) <= LINKEDIN_NOTE_LIMIT
    assert "a scheduler a scheduler" not in note  # generic variant, not a truncation


def test_rank_bullets_uses_the_recipients_background():
    """The background is the most specific signal available — it should be able to
    change which bullet is surfaced."""
    resume, bullets = _setup()
    person = PERSON.model_copy(
        update={"company_blurb": "", "background": "writing Java query planners"}
    )
    top = rank_bullets(resume, bullets, person, limit=1)
    assert "query planner" in top[0].text.lower()


def test_user_text_labels_missing_research_instead_of_omitting_it():
    """A silently absent field invites the model to fill the gap from its own
    knowledge of the company; an explicit '(not provided)' does not."""
    resume, bullets = _setup()
    text = _build_user_text(PERSON, bullets[:2], resume)
    assert text.count("(not provided") == 2
    filled = _build_user_text(RESEARCHED, bullets[:2], resume)
    assert "(not provided" not in filled
    assert HOOK in filled and BACKGROUND in filled


def test_message_in_the_intended_voice_survives_grounding():
    """The regression guard for the vocabulary widening.

    Grounding rejects a field on ONE unknown token and silently ships the
    template instead — so an ordinary, curiosity-led message written in the voice
    the prompt now asks for MUST pass, or the rewrite is invisible in production.
    """
    resume, bullets = _setup()
    top = rank_bullets(resume, bullets, RESEARCHED, limit=2)

    def fake_complete(system_blocks, user_text):
        return {
            "connect_note": "Hi Jane — I keep coming back to Robotics Co's work. "
                            "Would love to connect!",
            "message": (
                "Hi Jane, thanks for the connect!\n\n"
                "I'm Test Candidate, a computer science student who has been going "
                "deep on data infrastructure lately.\n\n"
                "I read up on Robotics Co and was genuinely surprised by "
                f"{HOOK} — I had assumed that was the whole hard part. I "
                f"also noticed your background in {BACKGROUND}, and I'm curious how "
                "much of that expertise translates to the problems you are working "
                "on today.\n\n"
                "Would you have 15 minutes at your convenience for a quick call?\n\n"
                "Thanks for your time,\nTest Candidate"
            ),
        }

    note, message = draft_networking_copy(
        person=RESEARCHED, resume=resume, top_bullets=top, complete=fake_complete
    )
    assert message.used_llm is True, "the intended voice must not fail grounding"
    assert note.used_llm is True

"""Phase 6b — the email escalation: the drafter grounds like 6a, the Gmail-draft
step is verified-address-only + CAN-SPAM gated + idempotent, the planner only
escalates when the flag is on, and the state machine gains email_drafted/email_sent.
All offline: deterministic copy, a fake draft_fn, SQLite storage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.networking.copy import (
    deterministic_email,
    draft_networking_email,
)
from internship_pipeline.networking.email import (
    create_networking_email_drafts,
    eligible_for_email_draft,
)
from internship_pipeline.networking.models import (
    STATUS_ACCEPTED,
    STATUS_EMAIL_DRAFTED,
    STATUS_EMAIL_DUE,
    STATUS_EMAIL_SENT,
    STATUS_MESSAGE_SENT,
    Person,
    allowed_human_transition,
)
from internship_pipeline.networking.sequence import (
    DRAFT_EMAIL,
    outstanding_actions,
    plan_due,
)
from internship_pipeline.outreach.footer import OPT_OUT_MARKER
from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.storage import get_storage

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")

PERSON = Person(
    person_id="test-robotics-co-1",
    company_name="Robotics Co",
    company_domain="roboticsco.com",
    company_blurb="Robotics Co builds data infrastructure for warehouse robots in Python.",
    name="Jane Doe",
    role="CTO",
    status=STATUS_EMAIL_DUE,
)


def _setup():
    resume = load_master_resume(FIXTURE)
    return resume, all_bullets(resume)


# --------------------------------------------------------------------------- #
# The drafter
# --------------------------------------------------------------------------- #
def test_deterministic_email_is_real_and_references_linkedin_and_bullet():
    resume, bullets = _setup()
    subject, body = deterministic_email(PERSON, resume, bullets[:2])
    assert "Robotics Co" in subject
    assert len(subject) <= 70
    assert "LinkedIn" in body  # acknowledges the earlier outreach
    assert "Robotics Co" in body
    assert bullets[0].text.replace("**", "") in body  # a real bullet, verbatim
    assert body.strip().endswith("Test Candidate")  # signs with the real name


def test_no_llm_returns_deterministic_email():
    resume, bullets = _setup()
    email = draft_networking_email(person=PERSON, resume=resume, top_bullets=bullets[:2])
    assert email.used_llm is False
    assert (email.subject, email.body) == deterministic_email(PERSON, resume, bullets[:2])


def test_grounded_llm_email_is_kept():
    resume, bullets = _setup()

    def fake_complete(system_blocks, user_text):
        return {
            "subject": "Reaching out about Robotics Co",
            "body": (
                "Hi Jane, I reached out on LinkedIn recently and wanted to email you "
                "directly. I built a data pipeline in Python that maps to Robotics Co's "
                "work. Would love to connect about interns next summer."
            ),
        }

    email = draft_networking_email(
        person=PERSON, resume=resume, top_bullets=bullets[:2], complete=fake_complete
    )
    assert email.used_llm is True
    assert "data pipeline" in email.body
    assert email.subject == "Reaching out about Robotics Co"


def test_fabricated_llm_email_falls_back_per_field():
    resume, bullets = _setup()

    def fake_complete(system_blocks, user_text):
        return {
            "subject": "Reaching out about Robotics Co",  # grounded → kept
            "body": "I boosted revenue 400% at Google using Rust.",  # fabricated → dropped
        }

    email = draft_networking_email(
        person=PERSON, resume=resume, top_bullets=bullets[:2], complete=fake_complete
    )
    # One field fell back, so the whole draft is marked deterministic.
    assert email.used_llm is False
    det_subject, det_body = deterministic_email(PERSON, resume, bullets[:2])
    assert email.body == det_body
    for token in ("Google", "Rust", "400"):
        assert token not in email.body


def test_llm_error_falls_back_to_deterministic_email():
    resume, bullets = _setup()

    def boom(system_blocks, user_text):
        raise RuntimeError("api down")

    email = draft_networking_email(
        person=PERSON, resume=resume, top_bullets=bullets[:2], complete=boom
    )
    assert email.used_llm is False
    assert email.subject and email.body


# --------------------------------------------------------------------------- #
# Planner: escalation only fires when enabled
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc)


def test_plan_due_no_email_escalation_when_disabled():
    due = plan_due(
        [PERSON], now=_now(), daily_connect_budget=5,
        accept_window_days=10, reply_window_days=7,  # email_escalation_enabled defaults off
    )
    assert not any(a.action == DRAFT_EMAIL for a in due)


def test_plan_due_escalates_email_due_when_enabled():
    due = plan_due(
        [PERSON], now=_now(), daily_connect_budget=5,
        accept_window_days=10, reply_window_days=7, email_escalation_enabled=True,
    )
    assert [a.person.person_id for a in due if a.action == DRAFT_EMAIL] == [PERSON.person_id]


def test_plan_due_skips_email_due_without_identity():
    faceless = PERSON.model_copy(update={"name": None, "linkedin_url": None})
    due = plan_due(
        [faceless], now=_now(), daily_connect_budget=5,
        accept_window_days=10, reply_window_days=7, email_escalation_enabled=True,
    )
    assert not any(a.action == DRAFT_EMAIL for a in due)


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
def test_human_transitions_for_email_states():
    # The human's "I sent it" flip is allowed from the drafted state...
    assert allowed_human_transition(STATUS_EMAIL_DRAFTED, STATUS_EMAIL_SENT)
    # ...and directly from email_due (they may have sent it out of band).
    assert allowed_human_transition(STATUS_EMAIL_DUE, STATUS_EMAIL_SENT)
    # A late accept still revives the LinkedIn path (the one backward exception).
    assert allowed_human_transition(STATUS_EMAIL_DUE, STATUS_ACCEPTED)
    # But you can't walk back from email_sent to a drafting state.
    assert not allowed_human_transition(STATUS_EMAIL_SENT, STATUS_EMAIL_DRAFTED)
    # email_drafted is pipeline-owned — the human can't set it directly.
    assert not allowed_human_transition(STATUS_MESSAGE_SENT, STATUS_EMAIL_DRAFTED)


# --------------------------------------------------------------------------- #
# outstanding_actions surfacing
# --------------------------------------------------------------------------- #
def test_outstanding_actions_email_drafted_with_gmail_link():
    person = PERSON.model_copy(update={
        "status": STATUS_EMAIL_DRAFTED, "draft_kind": "email",
        "gmail_draft_id": "d1", "gmail_draft_link": "https://mail.google.com/x",
    })
    actions = outstanding_actions([person], email_escalation_enabled=True)
    assert len(actions) == 1
    assert actions[0].kind == "email"
    assert "Gmail" in actions[0].instruction


def test_outstanding_actions_email_drafted_without_verified_address_shows_guess():
    person = PERSON.model_copy(update={"status": STATUS_EMAIL_DRAFTED, "draft_kind": "email"})
    actions = outstanding_actions([person], email_escalation_enabled=True)
    assert actions[0].kind == "email"
    # Best-guess pattern from the company domain + the person's name.
    assert "jane.doe@roboticsco.com" in actions[0].instruction


def test_outstanding_actions_email_due_text_depends_on_flag():
    person = PERSON.model_copy(update={"status": STATUS_EMAIL_DUE})
    on = outstanding_actions([person], email_escalation_enabled=True)[0]
    off = outstanding_actions([person], email_escalation_enabled=False)[0]
    assert "drafted on the next run" in on.instruction
    assert "nudge manually" in off.instruction


# --------------------------------------------------------------------------- #
# The Gmail-draft step (verified-only, CAN-SPAM gated, idempotent)
# --------------------------------------------------------------------------- #
@pytest.fixture
def store_settings(tmp_path):
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        outreach_from_name="Test Candidate",
        outreach_from_email="candidate@example.com",
        outreach_physical_address="123 Example St, Boston MA",
    )
    return get_storage(settings), settings


def _drafted_person(**overrides) -> Person:
    base = dict(
        person_id="test-robotics-co-1", company_name="Robotics Co",
        status=STATUS_EMAIL_DRAFTED, draft_kind="email",
        draft_subject="Reaching out about Robotics Co",
        draft_body="Hi Jane,\n\n...\n\n--\nTo opt out, reply STOP.",
        email="jane@roboticsco.com",
    )
    base.update(overrides)
    return Person(**base)


def test_eligibility_requires_verified_address_and_no_existing_draft():
    assert eligible_for_email_draft(_drafted_person())
    assert not eligible_for_email_draft(_drafted_person(email=None))  # guessed only
    assert not eligible_for_email_draft(_drafted_person(gmail_draft_id="already"))
    assert not eligible_for_email_draft(_drafted_person(status=STATUS_EMAIL_DUE))


def test_create_drafts_records_id_and_link_and_is_idempotent(store_settings):
    storage, settings = store_settings
    person = _drafted_person()
    storage.save_person(person)
    calls: list[tuple] = []

    def fake_draft(sender, to, subject, body):
        calls.append((sender, to, subject, body))
        return "draft-1", "msg-1"

    created = create_networking_email_drafts(
        [person], settings=settings, storage=storage, draft_fn=fake_draft
    )
    assert created == 1
    assert calls[0][0] == "Test Candidate <candidate@example.com>"
    assert calls[0][1] == "jane@roboticsco.com"
    saved = storage.get_person("test-robotics-co-1")
    assert saved.gmail_draft_id == "draft-1"
    assert "draft-1" not in (saved.gmail_draft_link or "") or saved.gmail_draft_link  # link set
    assert saved.gmail_draft_link and "mail.google.com" in saved.gmail_draft_link

    # Second pass: the row now carries a draft id → skipped (no duplicate Gmail draft).
    again = create_networking_email_drafts(
        [saved], settings=settings, storage=storage, draft_fn=fake_draft
    )
    assert again == 0
    assert len(calls) == 1


def test_placeholder_address_blocks_all_drafts(store_settings):
    storage, settings = store_settings
    settings = settings.model_copy(update={
        "outreach_physical_address": "REPLACE_ME — your physical mailing address",
    })
    person = _drafted_person()
    storage.save_person(person)
    created = create_networking_email_drafts(
        [person], settings=settings, storage=storage, draft_fn=lambda *a: ("x", "y"),
    )
    assert created == 0
    assert storage.get_person("test-robotics-co-1").gmail_draft_id is None


def test_suppressed_recipient_is_skipped(store_settings):
    storage, settings = store_settings
    storage.add_suppression("jane@roboticsco.com", reason="asked to stop")
    person = _drafted_person()
    storage.save_person(person)
    created = create_networking_email_drafts(
        [person], settings=settings, storage=storage, draft_fn=lambda *a: ("x", "y"),
    )
    assert created == 0


def test_footer_marker_present_in_baked_body():
    # Sanity: the opt-out marker the drafts rely on is a real, importable constant
    # (the stage bakes the footer into draft_body before this step runs).
    assert OPT_OUT_MARKER == "To opt out"


def test_no_circular_import_entering_via_storage():
    # Regression: storage.base imports networking.models, which runs
    # networking/__init__ — so if that __init__ pulled in networking.email (which
    # imports storage back), a process first touching storage (as run_daily does)
    # would hit a partially-initialized-module ImportError. Reproduce in a FRESH
    # interpreter (this session already has everything cached).
    import subprocess
    import sys

    code = (
        "import internship_pipeline.storage;"  # enter through storage FIRST
        "import internship_pipeline.networking.email as e;"
        "assert hasattr(e, 'create_networking_email_drafts');"
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout

"""Re-drafting rows already in flight: the ladder itself never revisits them.

``plan_due`` skips a person at ``connect_drafted``/``message_drafted`` — that row
waits on the human — so an improvement to the prompt, the grounding vocabulary or
the roster's research never reaches the drafts already sitting on the sheet.
``redraft`` is the maintenance pass that does; it must rewrite the copy and touch
nothing else about the row's ladder position.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.networking.models import (
    STATUS_ACCEPTED,
    STATUS_CONNECT_DRAFTED,
    STATUS_EMAIL_DRAFTED,
    STATUS_MESSAGE_DRAFTED,
    STATUS_QUEUED,
    Person,
)
from internship_pipeline.networking.redraft import redraft_people, redraftable

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")
CHANGED_AT = "2026-08-01T12:00:00+00:00"


def _settings() -> Settings:
    return Settings(_env_file=None, master_resume_file=FIXTURE)


def _person(person_id: str, status: str, **kw) -> Person:
    return Person(
        person_id=person_id,
        company_name="Robotics Co",
        company_blurb="Robotics Co builds data infrastructure for warehouse robots.",
        company_hook="their scheduler reassigning work mid-shift",
        name="Jane Doe",
        role="CTO",
        background="building the onboard perception stack",
        status=status,
        status_changed_at=CHANGED_AT,
        draft_body="stale copy from an older prompt",
        **kw,
    )


def test_only_drafted_rows_are_eligible():
    people = [
        _person("a", STATUS_CONNECT_DRAFTED),
        _person("b", STATUS_MESSAGE_DRAFTED),
        _person("c", STATUS_QUEUED),
        _person("d", STATUS_ACCEPTED),  # the ladder itself drafts this one next run
    ]
    assert [p.person_id for p in redraftable(people)] == ["a", "b"]
    assert [p.person_id for p in redraftable(people, kinds={"connect"})] == ["a"]


def test_an_email_already_in_gmail_drafts_is_left_alone():
    """Its text is out in a draft the human may be editing — rewriting the stored
    body would only desynchronize the two."""
    sent = _person("a", STATUS_EMAIL_DRAFTED, draft_kind="email", gmail_draft_id="d1")
    waiting = _person("b", STATUS_EMAIL_DRAFTED, draft_kind="email")
    assert [p.person_id for p in redraftable([sent, waiting])] == ["b"]


def test_redraft_rewrites_the_copy_and_moves_nothing_else():
    person = _person("a", STATUS_MESSAGE_DRAFTED, draft_kind="message")
    results = redraft_people([person], settings=_settings())

    assert len(results) == 1 and results[0].changed
    assert person.draft_body != "stale copy from an older prompt"
    assert person.company_hook in person.draft_body  # the newer, researched style
    assert person.background in person.draft_body
    # Ladder position is untouched: same status, same clock (the escalation
    # timers measure from status_changed_at and must not restart).
    assert person.status == STATUS_MESSAGE_DRAFTED
    assert person.status_changed_at == CHANGED_AT
    assert person.draft_kind == "message"


def test_each_status_gets_its_own_artifact():
    connect = _person("a", STATUS_CONNECT_DRAFTED, draft_kind="connect")
    message = _person("b", STATUS_MESSAGE_DRAFTED, draft_kind="message")
    redraft_people([connect, message], settings=_settings())
    # The connect note is the one-liner under LinkedIn's cap; the message is prose.
    assert "\n" not in connect.draft_body
    assert connect.draft_body != message.draft_body
    assert message.draft_body.count("\n") > 2


def test_dry_run_computes_without_saving():
    person = _person("a", STATUS_MESSAGE_DRAFTED, draft_kind="message")
    saved: list[Person] = []

    class _Store:
        def save_person(self, p):  # pragma: no cover - must never be called
            saved.append(p)

    results = redraft_people([person], settings=_settings(), storage=None)
    assert results and results[0].after
    assert saved == []

    redraft_people([person], settings=_settings(), storage=_Store())
    assert [p.person_id for p in saved] == ["a"]


def test_nothing_eligible_skips_the_resume_load_entirely():
    """Zero-credential/zero-work path: no master résumé needed to do nothing."""
    settings = Settings(_env_file=None, master_resume_file="does-not-exist.yaml")
    assert redraft_people([_person("a", STATUS_QUEUED)], settings=settings) == []
    with pytest.raises(FileNotFoundError):
        redraft_people([_person("a", STATUS_CONNECT_DRAFTED)], settings=settings)

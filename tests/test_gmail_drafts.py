"""Phase 5: Gmail drafts for outreach (gating + status transitions) and reply
correlation. All offline — the draft function is injected, never the live API."""

from __future__ import annotations

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.models import Outreach, make_outreach_id
from internship_pipeline.outreach.drafts import create_gmail_drafts, eligible_for_gmail_draft
from internship_pipeline.outreach.replies import Reply, correlate_replies
from internship_pipeline.storage import SQLiteStore


@pytest.fixture
def storage(tmp_path) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / "t.db"))


def _settings(**over) -> Settings:
    return Settings(_env_file=None, outreach_from_name="Paul", outreach_from_email="p@x.com", **over)


def _outreach(key: str, channel: str = "email", **over) -> Outreach:
    defaults = dict(
        outreach_id=make_outreach_id(key, channel),
        dedupe_key=key,
        company_name="Acme",
        title="Backend Intern",
        url="https://acme/1",
        channel=channel,
        contact_email="r@acme.com",
        contact_verified=True,
        subject="Hello",
        body="short note\n--\nfooter",
    )
    defaults.update(over)
    return Outreach(**defaults)


def test_eligibility_gates():
    assert eligible_for_gmail_draft(_outreach("k1"))
    assert not eligible_for_gmail_draft(_outreach("k2", channel="linkedin"))  # never LinkedIn
    assert not eligible_for_gmail_draft(_outreach("k3", contact_verified=False))  # verified only
    assert not eligible_for_gmail_draft(_outreach("k4", contact_email=None))
    assert not eligible_for_gmail_draft(_outreach("k5", suppressed=True))
    assert not eligible_for_gmail_draft(_outreach("k6", status="sent"))
    assert not eligible_for_gmail_draft(_outreach("k7", gmail_draft_id="d1"))  # idempotent


def test_create_drafts_transitions_status_and_persists(storage):
    calls: list[tuple] = []

    def fake_draft(sender, to, subject, body):
        calls.append((sender, to, subject, body))
        return "draft-1", "msg-1"

    o = _outreach("k1")
    storage.save_outreach(o)
    created = create_gmail_drafts([o], settings=_settings(), storage=storage, draft_fn=fake_draft)

    assert created == 1
    assert calls[0][0] == "Paul <p@x.com>" and calls[0][1] == "r@acme.com"
    stored = storage.get_outreach(o.outreach_id)
    assert stored.status == "gmail_draft_created"
    assert stored.gmail_draft_id == "draft-1"
    assert "msg-1" in stored.gmail_draft_link


def test_unverified_contact_never_becomes_a_draft(storage):
    o = _outreach("k1", contact_verified=False, contact_source="pattern_guess")
    storage.save_outreach(o)
    created = create_gmail_drafts(
        [o], settings=_settings(), storage=storage,
        draft_fn=lambda *a: pytest.fail("must not be called"),
    )
    assert created == 0
    assert storage.get_outreach(o.outreach_id).status == "pending_review"


def test_draft_failure_leaves_row_pending(storage):
    def boom(*a):
        raise RuntimeError("api down")

    o = _outreach("k1")
    storage.save_outreach(o)
    assert create_gmail_drafts([o], settings=_settings(), storage=storage, draft_fn=boom) == 0
    assert storage.get_outreach(o.outreach_id).status == "pending_review"


def test_no_sender_identity_skips_everything(storage):
    o = _outreach("k1")
    created = create_gmail_drafts(
        [o], settings=Settings(_env_file=None), storage=storage,
        draft_fn=lambda *a: pytest.fail("must not be called"),
    )
    assert created == 0


def test_correlate_replies_matches_sent_rows_by_email():
    sent = _outreach("k1", status="sent")
    pending = _outreach("k2", status="pending_review")
    replies = [Reply(message_id="m1", from_email="R@ACME.com")]
    matched = correlate_replies(replies, [sent, pending])
    assert matched == [sent]  # case-insensitive; only sent rows correlate
    assert correlate_replies([Reply(message_id="m2", from_email="other@x.com")], [sent]) == []

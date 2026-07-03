"""The manual approve-and-send gate: every refusal path, and one fake-sender success.

A real email is never sent — the transmit is an injected ``SendFn`` that just records
its arguments. The default (no ``--yes``/confirm) is always a preview.
"""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.models import Outreach, make_outreach_id
from internship_pipeline.outreach.approve_and_send import send_outreach
from internship_pipeline.outreach.footer import build_email_body
from internship_pipeline.storage.sqlite_store import SQLiteStore

REAL = dict(outreach_from_name="Ada", outreach_from_email="ada@x.com",
            outreach_physical_address="1 Real St, Townsville")


def _settings(**over) -> Settings:
    base = dict(REAL)
    base.update(over)
    return Settings(_env_file=None, **base)


def _email(settings, **over) -> Outreach:
    body = over.pop("body", build_email_body("Hi Bob, quick note about the role.", settings))
    base = dict(
        outreach_id=make_outreach_id("job1", "email"),
        dedupe_key="job1", company_name="Acme", title="Backend Intern",
        url="https://acme.com/1", channel="email",
        contact_email="bob@acme.com", contact_source="hunter", contact_verified=True,
        subject="Hello from Ada", body=body, status="pending_review",
    )
    base.update(over)
    return Outreach(**base)


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / "p.db"))


class _Recorder:
    def __init__(self):
        self.args = None

    def __call__(self, sender, to, subject, body):
        self.args = (sender, to, subject, body)
        return "msg-123"


def test_refuses_linkedin_channel(tmp_path):
    s, store = _settings(), _store(tmp_path)
    store.save_outreach(_email(s, outreach_id=make_outreach_id("job1", "linkedin"), channel="linkedin"))
    out = send_outreach(make_outreach_id("job1", "linkedin"), settings=s, storage=store, confirm=True)
    assert out.sent is False and out.status == "refused_linkedin"


def test_refuses_when_no_recipient(tmp_path):
    s, store = _settings(), _store(tmp_path)
    store.save_outreach(_email(s, contact_email=None))
    out = send_outreach(make_outreach_id("job1", "email"), settings=s, storage=store, confirm=True)
    assert out.sent is False and out.status == "no_recipient"


def test_refuses_and_flags_suppressed_recipient(tmp_path):
    s, store = _settings(), _store(tmp_path)
    store.add_suppression("acme.com")  # whole domain on the do-not-contact list
    store.save_outreach(_email(s))
    out = send_outreach(make_outreach_id("job1", "email"), settings=s, storage=store, confirm=True)
    assert out.sent is False and out.status == "suppressed"
    # the flag is persisted so the tracker reflects reality
    assert store.get_outreach(make_outreach_id("job1", "email")).suppressed is True


def test_refuses_missing_footer(tmp_path):
    s, store = _settings(), _store(tmp_path)
    store.save_outreach(_email(s, body="No footer here."))
    out = send_outreach(make_outreach_id("job1", "email"), settings=s, storage=store, confirm=True)
    assert out.sent is False and out.status == "missing_footer"


def test_refuses_placeholder_physical_address(tmp_path):
    # Footer present, but the CAN-SPAM physical address is still the placeholder.
    placeholder = Settings(_env_file=None, outreach_from_name="Ada", outreach_from_email="ada@x.com")
    store = _store(tmp_path)
    store.save_outreach(_email(placeholder))
    out = send_outreach(make_outreach_id("job1", "email"), settings=placeholder, storage=store, confirm=True)
    assert out.sent is False and out.status == "placeholder_address"


def test_default_is_preview_only(tmp_path):
    s, store = _settings(), _store(tmp_path)
    store.save_outreach(_email(s))
    out = send_outreach(make_outreach_id("job1", "email"), settings=s, storage=store)  # confirm defaults False
    assert out.sent is False and out.status == "preview"


def test_confirmed_send_uses_injected_sender_and_records_result(tmp_path):
    s, store = _settings(), _store(tmp_path)
    store.save_outreach(_email(s))
    rec = _Recorder()
    out = send_outreach(make_outreach_id("job1", "email"), settings=s, storage=store,
                        send_fn=rec, confirm=True)
    assert out.sent is True and out.status == "sent"
    sender, to, subject, body = rec.args
    assert to == "bob@acme.com" and subject == "Hello from Ada" and "ada@x.com" in sender
    saved = store.get_outreach(make_outreach_id("job1", "email"))
    assert saved.status == "sent" and saved.provider_message_id == "msg-123" and saved.sent_at

    # A second attempt refuses to re-send.
    again = send_outreach(make_outreach_id("job1", "email"), settings=s, storage=store,
                          send_fn=rec, confirm=True)
    assert again.sent is False and again.status == "already_sent"

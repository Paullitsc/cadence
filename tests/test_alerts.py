"""Failure alerts: email is implemented (via an injected Gmail send), slack is a stub."""

from __future__ import annotations

from internship_pipeline.alerts import send_alert, send_email_alert, send_slack_alert
from internship_pipeline.config import Settings


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_email_alert_sends_via_injected_gmail_fn():
    calls = []
    ok = send_email_alert("boom", _s(outreach_from_email="me@x.com"),
                          send_fn=lambda *a: (calls.append(a) or "mid-1"))
    assert ok is True
    sender, to, subject, body = calls[0]
    assert to == "me@x.com" and "boom" in body and "alert" in subject.lower()


def test_email_alert_without_recipient_is_a_noop():
    assert send_email_alert("boom", _s()) is False


def test_email_alert_without_gmail_is_a_noop():
    # recipient set, but no send_fn and no Gmail token → degrades to False (never raises)
    assert send_email_alert("boom", _s(outreach_from_email="me@x.com")) is False


def test_slack_alert_is_a_stub():
    assert send_slack_alert("boom", _s(slack_webhook_url="https://hooks.example")) is False


def test_send_alert_routes_by_channel():
    assert send_alert("boom", _s(), channel="slack") is False       # stub
    assert send_alert("boom", _s(), channel="nonsense") is False    # unknown → noop

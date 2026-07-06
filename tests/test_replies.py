"""Recruiter-reply scan: pure header parsing + a fake Gmail service (no network)."""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.outreach.replies import (
    build_query,
    fetch_replies,
    parse_message,
    scan_replies,
)

MSG = {
    "id": "m1", "threadId": "t1", "snippet": "Thanks for reaching out!",
    "payload": {"headers": [
        {"name": "From", "value": "Ada Lovelace <ada@acme.com>"},
        {"name": "Subject", "value": "Re: your note"},
        {"name": "Date", "value": "Wed, 02 Jul 2026 09:00:00 +0000"},
    ]},
}


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _Messages:
    def list(self, **kwargs):
        return _Exec({"messages": [{"id": "m1"}]})

    def get(self, **kwargs):
        return _Exec(MSG)


class _FakeService:
    def users(self):
        return self

    def messages(self):
        return _Messages()


def test_parse_message_extracts_sender_subject_snippet():
    r = parse_message(MSG)
    assert r.from_email == "ada@acme.com" and r.from_name == "Ada Lovelace"
    assert r.subject == "Re: your note" and r.snippet == "Thanks for reaching out!"
    assert r.who == "Ada Lovelace"


def test_parse_message_tolerates_missing_headers():
    r = parse_message({"id": "m2"})
    assert r.who == "(unknown sender)" and r.subject == ""


def test_build_query_has_window_and_extra_terms():
    q = build_query(_s(reply_scan_days=10, reply_scan_query="subject:interview"))
    assert "newer_than:10d" in q and "-from:me" in q and "subject:interview" in q


def test_scan_replies_without_gmail_returns_empty():
    assert scan_replies(_s()) == []


def test_fetch_replies_hydrates_from_a_fake_service():
    out = fetch_replies(_FakeService(), "q", 5)
    assert len(out) == 1 and out[0].from_email == "ada@acme.com"

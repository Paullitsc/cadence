"""Digest content (Phase 4 email; Phase 5 slim/outreach-focused) + email gating."""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.digest import render_digest, render_digest_text, send_digest_email
from internship_pipeline.models import Job, Outreach, make_outreach_id
from internship_pipeline.outreach.replies import Reply


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def _fixtures():
    outreach = [Outreach(outreach_id=make_outreach_id("a1", "email"), dedupe_key="a1",
                         company_name="Acme", title="Backend Intern", url="https://acme/1",
                         channel="email", contact_name="Rae Recruiter",
                         contact_title="University Recruiter", contact_email="r@acme.com",
                         contact_source="hunter", contact_verified=True,
                         status="gmail_draft_created", gmail_draft_id="d1",
                         gmail_draft_link="https://mail.google.com/mail/u/0/#drafts?compose=m1")]
    replies = [Reply(message_id="m1", from_name="Ada Lovelace", subject="Re: your note")]
    return outreach, replies


def test_render_keeps_outreach_and_replies_drops_application_sections():
    outreach, replies = _fixtures()
    html = render_digest(
        jobs=[Job(company_name="Gamma", title="Intern", url="https://g/1")], run_id="rid",
        counts={"new": 1, "applications_prepared": 3, "llm_calls_saved": 2},
        pending_outreach=outreach, replies=replies,
        sheet_url="https://docs.google.com/spreadsheets/d/SID",
    )
    # Kept: outreach (enriched) + replies.
    assert "awaiting your approval" in html and "r@acme.com" in html
    assert "Rae Recruiter" in html and "University Recruiter" in html
    assert "hunter" in html and "verified" in html
    assert "https://mail.google.com/mail/u/0/#drafts?compose=m1" in html
    assert make_outreach_id("a1", "email") in html      # actionable id shown
    assert "possible" in html.lower() and "Ada Lovelace" in html
    # Slimmed: application sections moved to the sheet; header carries the counts.
    assert "Top matches by fit" not in html
    assert "awaiting your submit" not in html
    assert "New internships today" not in html
    assert "https://docs.google.com/spreadsheets/d/SID" in html
    assert "LLM calls saved" in html


def test_render_flags_unverified_contact():
    o = Outreach(outreach_id=make_outreach_id("b1", "email"), dedupe_key="b1",
                 company_name="Beta", title="Intern", url="https://b/1", channel="email",
                 contact_source="pattern_guess", contact_verified=False)
    html = render_digest(jobs=[], run_id="rid", pending_outreach=[o])
    assert "unverified" in html
    assert "pattern_guess" in html


def test_render_text_summary_counts():
    outreach, replies = _fixtures()
    text = render_digest_text(jobs=[], run_id="rid",
                              counts={"applications_prepared": 3, "llm_calls_saved": 2},
                              pending_outreach=outreach, replies=replies,
                              sheet_url="https://docs.google.com/spreadsheets/d/SID")
    assert "Outreach drafts awaiting approval: 1" in text
    assert "Applications prepared: 3" in text
    assert "LLM calls saved (CV grouping): 2" in text
    assert "https://docs.google.com/spreadsheets/d/SID" in text


def test_digest_email_disabled_by_default():
    assert send_digest_email(html="<p>x</p>", text="x",
                             settings=_s(outreach_from_email="me@x.com")) is False


def test_digest_email_sends_when_enabled_with_injected_fn():
    calls = []
    ok = send_digest_email(
        html="<p>hi</p>", text="hi",
        settings=_s(digest_email_enabled=True, outreach_from_email="me@x.com"),
        send_fn=lambda s, t, subj, txt, h: (calls.append((s, t, subj, txt, h)) or "mid"),
    )
    assert ok is True
    sender, to, subject, txt, html = calls[0]
    assert to == "me@x.com" and html == "<p>hi</p>" and txt == "hi"

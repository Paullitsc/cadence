"""Phase 4 digest: all sections render, and the digest email is gated + fake-sendable."""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.digest import render_digest, render_digest_text, send_digest_email
from internship_pipeline.models import Application, Job, Outreach, make_outreach_id
from internship_pipeline.outreach.replies import Reply


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def _fixtures():
    apps = [Application(dedupe_key="a1", company_name="Acme", title="Backend Intern",
                        url="https://acme/1", fit_score=0.82, status="pending_review",
                        tailored_resume_path="a1.pdf", human_review=True)]
    outreach = [Outreach(outreach_id=make_outreach_id("a1", "email"), dedupe_key="a1",
                         company_name="Acme", title="Backend Intern", url="https://acme/1",
                         channel="email", contact_email="r@acme.com", contact_source="hunter",
                         contact_verified=True)]
    replies = [Reply(message_id="m1", from_name="Ada Lovelace", subject="Re: your note")]
    return apps, outreach, replies


def test_render_shows_every_section():
    apps, outreach, replies = _fixtures()
    html = render_digest(
        jobs=[Job(company_name="Gamma", title="Intern", url="https://g/1")], run_id="rid",
        top_applications=apps, pending_outreach=outreach, pending_applications=apps, replies=replies,
    )
    assert "Top matches by fit" in html
    assert "awaiting your approval" in html and "r@acme.com" in html
    assert make_outreach_id("a1", "email") in html      # actionable id shown
    assert "awaiting your submit" in html
    assert "possible" in html.lower() and "Ada Lovelace" in html
    assert "New internships today — 1" in html


def test_render_text_summary_counts():
    apps, outreach, replies = _fixtures()
    text = render_digest_text(jobs=[], run_id="rid", top_applications=apps,
                              pending_outreach=outreach, pending_applications=apps, replies=replies)
    assert "Outreach drafts awaiting approval: 1" in text


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

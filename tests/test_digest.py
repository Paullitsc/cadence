from __future__ import annotations

from internship_pipeline.digest import render_digest, write_digest
from internship_pipeline.models import Job, JobSource, Outreach, make_outreach_id


def _jobs() -> list[Job]:
    return [
        Job(
            company_name="Beta Inc",
            title="ML Intern",
            url="https://beta/1",
            locations=["Remote"],
            source_feed=JobSource.ASHBY,
        ),
        Job(
            company_name="Acme",
            title="SWE Intern",
            url="https://acme/1",
            locations=["NYC"],
            source_feed=JobSource.GREENHOUSE,
        ),
    ]


def test_render_header_counts_new_jobs():
    # Phase 5: the digest no longer LISTS jobs — the header counts them and the
    # sheet is the workspace. The count falls back to len(jobs) when not passed.
    html = render_digest(jobs=_jobs(), run_id="r1")
    assert "new jobs" in html
    assert "<b>2</b>" in html
    assert "SWE Intern" not in html  # no per-job listing anymore


def test_render_empty_state():
    html = render_digest(jobs=[], run_id="r1")
    assert "<b>0</b>" in html
    assert "No outreach drafts awaiting approval" in html


def test_render_sheet_link_and_unconfigured_hint():
    html = render_digest(jobs=[], run_id="r1", sheet_url="https://docs.google.com/spreadsheets/d/SID")
    assert "https://docs.google.com/spreadsheets/d/SID" in html
    assert "application tracker" in html.lower()
    html_off = render_digest(jobs=[], run_id="r1")
    assert "not configured" in html_off


def test_write_digest_creates_dated_and_latest(tmp_path):
    html = render_digest(jobs=_jobs(), run_id="r1")
    path = write_digest(html, str(tmp_path / "digests"), date="20260629")
    assert path.name == "digest-20260629.html"
    assert path.exists()
    assert (tmp_path / "digests" / "latest.html").read_text(encoding="utf-8") == html


def test_render_escapes_html():
    evil = Outreach(
        outreach_id=make_outreach_id("k1", "email"), dedupe_key="k1",
        company_name="Evil <script>", title="x", url="https://e/1", channel="email",
    )
    html = render_digest(jobs=[], run_id="r1", pending_outreach=[evil])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html

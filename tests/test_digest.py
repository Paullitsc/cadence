from __future__ import annotations

from internship_pipeline.digest import render_digest, write_digest
from internship_pipeline.models import Job, JobSource


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


def test_render_includes_jobs_and_count():
    html = render_digest(jobs=_jobs(), run_id="r1", counts={"total_sourced": 5})
    assert "SWE Intern" in html
    assert "ML Intern" in html
    assert "https://acme/1" in html
    assert "New internships today — 2" in html
    # Grouped + sorted by company: Acme appears before Beta Inc.
    assert html.index("Acme") < html.index("Beta Inc")


def test_render_empty_state():
    html = render_digest(jobs=[], run_id="r1")
    assert "No new active roles" in html
    assert "New internships today — 0" in html


def test_write_digest_creates_dated_and_latest(tmp_path):
    html = render_digest(jobs=_jobs(), run_id="r1")
    path = write_digest(html, str(tmp_path / "digests"), date="20260629")
    assert path.name == "digest-20260629.html"
    assert path.exists()
    assert (tmp_path / "digests" / "latest.html").read_text(encoding="utf-8") == html


def test_render_escapes_html():
    job = Job(company_name="Evil <script>", title="x", url="https://e/1")
    html = render_digest(jobs=[job], run_id="r1")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html

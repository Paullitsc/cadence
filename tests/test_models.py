from __future__ import annotations

import json
from pathlib import Path

from internship_pipeline.models import Job, JobSource, normalize_url

FIXTURE = Path(__file__).parent / "fixtures" / "listings_sample.json"


def _load() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def test_job_parses_fixture():
    jobs = [Job(**row) for row in _load()]
    assert jobs[0].company_name == "Example Corp"
    assert jobs[0].locations == ["Remote", "New York, NY"]
    assert jobs[0].active is True


def test_locations_coerced_from_bare_string():
    job = Job(**_load()[1])
    assert job.locations == ["San Francisco, CA"]


def test_date_posted_coerced_to_str():
    # Fixture has an int epoch; the model stores it as raw text (parsed in Phase 1).
    job = Job(**_load()[1])
    assert job.date_posted == "1717200000"


def test_normalize_url_lowercases_host_and_strips_trailing_slash():
    a = normalize_url("https://Example.com/Jobs/123/")
    b = normalize_url("https://example.com/Jobs/123")
    assert a == b == "https://example.com/Jobs/123"


def test_normalize_url_strips_tracking_params():
    bare = normalize_url("https://example.com/jobs/123")
    tracked = normalize_url("https://example.com/jobs/123?utm_source=Simplify&ref=Simplify")
    assert bare == tracked


def test_normalize_url_keeps_identity_params():
    a = normalize_url("https://job-boards.greenhouse.io/acme/jobs/123?gh_jid=123")
    b = normalize_url("https://job-boards.greenhouse.io/acme/jobs/123?gh_jid=456")
    assert a != b


def test_dedupe_key_ignores_tracking_params():
    bare = Job(company_name="A", title="X", url="https://example.com/jobs/123")
    tracked = Job(
        company_name="A", title="X",
        url="https://example.com/jobs/123?utm_source=Simplify&ref=Simplify",
    )
    assert bare.dedupe_key() == tracked.dedupe_key()


def test_dedupe_key_stable_and_distinct():
    j1 = Job(company_name="A", title="X", url="https://example.com/jobs/123")
    j2 = Job(company_name="A", title="X", url="https://example.com/jobs/123/")
    j3 = Job(company_name="A", title="X", url="https://example.com/jobs/999")
    assert j1.dedupe_key() == j2.dedupe_key()
    assert j1.dedupe_key() != j3.dedupe_key()
    assert len(j1.dedupe_key()) == 16


def test_jobsource_values():
    assert JobSource.GREENHOUSE.value == "greenhouse"
    assert JobSource("lever") is JobSource.LEVER

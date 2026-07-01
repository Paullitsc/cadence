from __future__ import annotations

import json
from pathlib import Path

from internship_pipeline.models import JobSource
from internship_pipeline.sourcing.ats import (
    parse_ashby,
    parse_greenhouse,
    parse_lever,
)


def load_fixture(name: str):
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_greenhouse_normalizes_and_skips_incomplete_rows():
    jobs = parse_greenhouse(
        load_fixture("greenhouse_sample.json"), slug="example", company_name="Fallback"
    )
    # The third row has no url -> skipped.
    assert len(jobs) == 2
    j = jobs[0]
    assert j.company_name == "Example Corp"  # from payload, not the fallback
    assert j.title == "Software Engineer Intern"
    assert j.url == "https://boards.greenhouse.io/example/jobs/1"
    assert j.locations == ["Remote"]
    assert j.date_posted == "2026-05-20T09:00:00-04:00"  # first_published preferred
    assert j.active is True
    assert j.source == "greenhouse:example"
    assert j.source_feed is JobSource.GREENHOUSE


def test_lever_uses_company_name_and_all_locations():
    jobs = parse_lever(
        load_fixture("lever_sample.json"), slug="example", company_name="Example Co"
    )
    # Third row has no hostedUrl/applyUrl -> skipped.
    assert len(jobs) == 2
    j = jobs[0]
    assert j.company_name == "Example Co"  # Lever feed carries no company name
    assert j.title == "Backend Engineering Intern"
    assert j.url == "https://jobs.lever.co/example/abc-123"
    assert j.locations == ["San Francisco", "Remote - US"]
    assert j.date_posted == "1781109739214"  # epoch ms coerced to str
    assert j.source_feed is JobSource.LEVER
    # Falls back to single `location` when allLocations is absent.
    assert jobs[1].locations == ["London"]


def test_ashby_filters_unlisted_and_flattens_secondary_locations():
    jobs = parse_ashby(
        load_fixture("ashby_sample.json"), slug="example", company_name="Example Co"
    )
    # The unlisted (isListed=false) row is dropped.
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Machine Learning Intern"
    assert j.url == "https://jobs.ashbyhq.com/example/a1"
    assert j.locations == ["Toronto, ON", "Remote (Canada)"]
    assert j.date_posted == "2026-06-10T17:21:26.410+00:00"
    assert j.active is True
    assert j.source_feed is JobSource.ASHBY


def test_empty_payloads_yield_no_jobs():
    assert parse_greenhouse({}, slug="x", company_name="x") == []
    assert parse_lever([], slug="x", company_name="x") == []
    assert parse_ashby({"jobs": []}, slug="x", company_name="x") == []

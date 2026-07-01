from __future__ import annotations

import json
from pathlib import Path

from internship_pipeline.models import JobSource
from internship_pipeline.sourcing.jsearch import parse_jsearch


def load_fixture(name: str):
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_jsearch_normalizes_and_skips_rows_without_company():
    jobs = parse_jsearch(load_fixture("jsearch_sample.json"))
    # Second row has null employer_name -> skipped.
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company_name == "Example Corp"
    assert j.title == "Software Engineering Intern"
    assert j.url == "https://example.com/apply/j1"
    assert j.locations == ["Seattle, WA, US"]
    assert j.source_feed is JobSource.JSEARCH


def test_jsearch_empty_payload():
    assert parse_jsearch({}) == []
    assert parse_jsearch({"data": {"jobs": []}}) == []


def test_jsearch_accepts_legacy_flat_data_shape():
    # Older ``/search`` shape was `{"data": [...]}` (no "jobs" wrapper).
    # search-v2's `{"data": {"jobs": [...]}}` is the current live shape, but
    # parsing stays tolerant of the flat list too.
    legacy = {
        "data": [
            {
                "employer_name": "Example Corp",
                "job_title": "Intern",
                "job_apply_link": "https://example.com/apply/legacy",
            }
        ]
    }
    jobs = parse_jsearch(legacy)
    assert len(jobs) == 1
    assert jobs[0].url == "https://example.com/apply/legacy"

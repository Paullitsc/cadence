from __future__ import annotations

from internship_pipeline.models import JobSource
from internship_pipeline.sourcing.landedhq import parse_landedhq


def test_parses_internship_rows_and_maps_fields():
    rows = [
        {
            "id": 1,
            "company": "Example Corp",
            "role": "Software Engineering Intern",
            "datePosted": "2026-07-01T00:00:00Z",
            "applyUrl": "https://x/1",
            "type": "SWE",
            "category": "Internship",
        },
    ]
    jobs = parse_landedhq(rows)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company_name == "Example Corp"
    assert j.title == "Software Engineering Intern"
    assert j.url == "https://x/1"
    assert j.date_posted == "2026-07-01T00:00:00Z"
    assert j.locations == []  # no location column in this feed
    assert j.source == "landedhq"
    assert j.source_feed is JobSource.LANDEDHQ


def test_new_grad_rows_are_skipped():
    rows = [
        {"company": "X", "role": "New Grad SWE", "applyUrl": "https://x/1", "category": "New Grad"},
        {"company": "X", "role": "Intern", "applyUrl": "https://x/2", "category": "Internship"},
    ]
    jobs = parse_landedhq(rows)
    assert len(jobs) == 1
    assert jobs[0].url == "https://x/2"


def test_missing_category_defaults_to_internship():
    rows = [{"company": "X", "role": "Intern", "applyUrl": "https://x/1"}]
    assert len(parse_landedhq(rows)) == 1


def test_lowercase_column_fallback():
    """PostgREST folds unquoted identifiers to lowercase — accept either casing."""
    rows = [{"company": "X", "role": "Intern", "applyurl": "https://x/1", "dateposted": "2026-07-01"}]
    jobs = parse_landedhq(rows)
    assert len(jobs) == 1
    assert jobs[0].url == "https://x/1"
    assert jobs[0].date_posted == "2026-07-01"


def test_rows_missing_required_fields_are_skipped():
    rows = [
        {"company": "X", "role": "No URL", "category": "Internship"},
        {"role": "No company", "applyUrl": "https://x/1", "category": "Internship"},
        {"company": "X", "applyUrl": "https://x/2", "category": "Internship"},  # no role
    ]
    assert parse_landedhq(rows) == []


def test_empty_and_none_rows():
    assert parse_landedhq([]) == []
    assert parse_landedhq(None) == []

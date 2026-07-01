from __future__ import annotations

import json
from pathlib import Path

from internship_pipeline.models import JobSource
from internship_pipeline.sourcing.simplify import parse_simplify


def load_fixture(name: str):
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_active_only_filters_inactive_rows():
    rows = load_fixture("listings_sample.json")
    jobs = parse_simplify(rows)  # active_only=True by default
    # Fixture has one active + one inactive (Placeholder Labs).
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company_name == "Example Corp"
    assert j.locations == ["Remote", "New York, NY"]
    assert j.source == "synthetic-fixture"
    assert j.source_feed is JobSource.SIMPLIFY


def test_active_only_false_keeps_all():
    rows = load_fixture("listings_sample.json")
    assert len(parse_simplify(rows, active_only=False)) == 2


def test_rows_missing_required_fields_are_skipped():
    rows = [
        {"company_name": "X", "title": "T", "url": "https://x/1", "active": True},
        {"company_name": "X", "title": "No URL", "active": True},  # no url -> skip
        {"title": "No company", "url": "https://x/2", "active": True},  # no company -> skip
    ]
    jobs = parse_simplify(rows)
    assert len(jobs) == 1
    assert jobs[0].url == "https://x/1"

from __future__ import annotations

from pathlib import Path

from internship_pipeline.models import JobSource
from internship_pipeline.sourcing.github_readme import parse_readme_internships, repo_slug


def load_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def test_parses_rows_and_skips_linkless_ones():
    jobs = parse_readme_internships(load_fixture("canadian_readme_sample.md"), source="fixture")
    # Fixture has 5 data rows; "No Link Corp" has no apply URL -> skipped.
    assert len(jobs) == 4
    assert all(j.source == "fixture" for j in jobs)
    assert all(j.source_feed is JobSource.GITHUB_README for j in jobs)
    assert all(j.active for j in jobs)


def test_continuation_row_inherits_previous_company():
    jobs = parse_readme_internships(load_fixture("canadian_readme_sample.md"), source="fixture")
    first, second = jobs[0], jobs[1]
    assert first.company_name == "Example Robotics"
    assert second.company_name == "Example Robotics"  # ↳ row
    assert second.title == "Cloud Associate Engineer"


def test_apply_url_is_outer_link_not_badge_image():
    jobs = parse_readme_internships(load_fixture("canadian_readme_sample.md"), source="fixture")
    assert jobs[0].url == "https://example.com/careers/rtos-intern"
    assert not any("img.shields.io" in j.url for j in jobs)


def test_dates_normalized_to_iso_and_unknown_kept_raw():
    jobs = parse_readme_internships(load_fixture("canadian_readme_sample.md"), source="fixture")
    assert jobs[0].date_posted == "2026-06-26"
    bold = next(j for j in jobs if j.company_name == "Bold Co")
    assert bold.date_posted == "TBD"  # unparseable -> kept raw, never invented


def test_multi_location_cell_is_split():
    jobs = parse_readme_internships(load_fixture("canadian_readme_sample.md"), source="fixture")
    maple = next(j for j in jobs if j.company_name == "Maple Analytics")
    assert maple.locations == ["Mississauga, ON", "Waterloo, ON"]


def test_markdown_emphasis_stripped_from_company():
    jobs = parse_readme_internships(load_fixture("canadian_readme_sample.md"), source="fixture")
    assert any(j.company_name == "Bold Co" for j in jobs)  # was **Bold Co**


def test_tables_without_internship_header_ignored():
    md = "| Foo | Bar |\n|---|---|\n| a | b |\n"
    assert parse_readme_internships(md, source="x") == []


def test_no_table_at_all_returns_empty():
    assert parse_readme_internships("# just prose\nnothing here\n", source="x") == []


def test_repo_slug_from_raw_url():
    url = "https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2026/main/README.md"
    assert repo_slug(url) == "negarprh/Canadian-Tech-Internships-2026"

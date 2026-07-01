from __future__ import annotations

import textwrap
from pathlib import Path

from internship_pipeline.sourcing.companies import CompanyTarget, load_companies


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_loads_valid_skips_placeholder_and_invalid(tmp_path):
    f = _write(
        tmp_path / "companies.yaml",
        """
        companies:
          - name: Greenhouse Co
            ats: greenhouse
            slug: realtoken
          - name: Lever Co
            ats: lever
            slug: leverslug
          - name: Ashby Co
            ats: ashby
            slug: ashbyslug
          - name: Placeholder
            ats: greenhouse
            slug: REPLACE_ME_GREENHOUSE
          - name: Bad ATS
            ats: workday
            slug: whatever
        """,
    )
    targets = load_companies(f)
    assert [t.slug for t in targets] == ["realtoken", "leverslug", "ashbyslug"]
    assert all(isinstance(t, CompanyTarget) for t in targets)


def test_missing_file_uses_fallback(tmp_path):
    fallback = _write(
        tmp_path / "companies.example.yaml",
        """
        companies:
          - name: FB
            ats: lever
            slug: fbslug
        """,
    )
    targets = load_companies(tmp_path / "nope.yaml", fallback=fallback)
    assert [t.slug for t in targets] == ["fbslug"]


def test_missing_file_and_no_fallback_returns_empty(tmp_path):
    assert load_companies(tmp_path / "nope.yaml") == []


def test_is_placeholder():
    assert CompanyTarget(name="x", ats="lever", slug="REPLACE_ME").is_placeholder
    assert not CompanyTarget(name="x", ats="lever", slug="realslug").is_placeholder

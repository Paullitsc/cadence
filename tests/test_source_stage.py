"""source stage: failed-source visibility (StageResult.ok / sources_failed).

A stage that catches its own per-feed errors still returns normally, so
these tests exercise the actual OUTCOME signal: `ok=False` only when every
attempted source failed, and `sources_failed` always reflects the count.
"""

from __future__ import annotations

import internship_pipeline.stages.source as source_stage
from internship_pipeline.config import Settings
from internship_pipeline.models import Job, JobSource, StageContext
from internship_pipeline.sourcing.simplify import parse_simplify


def _settings(tmp_path, **overrides) -> Settings:
    empty = tmp_path / "companies.yaml"
    empty.write_text("companies: []\n", encoding="utf-8")
    defaults = dict(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        companies_file=str(empty),
        enable_simplify=False,
        enable_jsearch=False,
        enable_github_readme=False,
        digest_dir=str(tmp_path / "digests"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _job(url: str) -> Job:
    return Job(company_name="Acme", title="Intern", url=url, source_feed=JobSource.SIMPLIFY)


def test_ok_true_when_no_sources_configured(tmp_path):
    ctx = StageContext(run_id="r1", settings=_settings(tmp_path))
    result = source_stage.run(ctx)
    assert result.ok is True
    assert result.counts["sources_failed"] == 0


def test_ok_false_when_every_source_fails(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enable_simplify=True, extra_listings_urls="")

    def boom(client, url, *, max_retries=3, active_only=True):
        raise RuntimeError("feed down")

    monkeypatch.setattr(source_stage, "fetch_simplify", boom)
    ctx = StageContext(run_id="r1", settings=settings)
    result = source_stage.run(ctx)

    assert result.ok is False
    assert result.counts["sources_failed"] == 1
    assert result.counts["jobs_sourced"] == 0


def test_ok_true_when_some_sources_succeed_and_others_fail(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        enable_simplify=True,
        extra_listings_urls="https://example.com/other-listings.json",
    )

    def flaky(client, url, *, max_retries=3, active_only=True):
        if "other-listings" in url:
            raise RuntimeError("this one is down")
        return parse_simplify(
            [{"company_name": "Acme", "title": "Intern", "url": "https://x/1", "active": True}]
        )

    monkeypatch.setattr(source_stage, "fetch_simplify", flaky)
    ctx = StageContext(run_id="r1", settings=settings)
    result = source_stage.run(ctx)

    assert result.ok is True  # at least one source succeeded
    assert result.counts["sources_failed"] == 1
    assert result.counts["jobs_sourced"] == 1

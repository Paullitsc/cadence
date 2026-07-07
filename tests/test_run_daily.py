from __future__ import annotations

import sqlite3

import pytest

from internship_pipeline import run_daily
from internship_pipeline.config import Settings
from internship_pipeline.models import StageContext, StageResult


@pytest.fixture
def offline_settings(tmp_path) -> Settings:
    """Settings that make the pipeline fully offline + deterministic.

    No companies, simplify/jsearch disabled (no network), sqlite + digest in tmp.
    """
    empty = tmp_path / "companies.yaml"
    empty.write_text("companies: []\n", encoding="utf-8")
    return Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        companies_file=str(empty),
        enable_simplify=False,
        enable_jsearch=False,
        enable_github_readme=False,
        digest_dir=str(tmp_path / "digests"),
    )


def test_run_all_stages_offline_succeeds(offline_settings):
    record = run_daily.run_pipeline(settings=offline_settings)
    assert record.status == "success"
    assert record.errors == []
    assert record.finished_at is not None
    # Phase 1 stages produce real (zero) counts with no sources configured.
    assert record.counts["jobs_sourced"] == 0
    assert record.counts["jobs_new"] == 0
    assert record.counts["new_jobs_today"] == 0
    assert record.counts["digest_written"] == 1
    # A digest file was written, and the run was persisted to the runs table.
    assert (offline_settings.digest_dir + "/latest.html")
    with sqlite3.connect(offline_settings.database_path) as conn:
        runs = conn.execute("SELECT run_id, status FROM runs").fetchall()
    assert len(runs) == 1
    assert runs[0][1] == "success"


def test_subset_runs_only_selected_stage(offline_settings):
    record = run_daily.run_pipeline(stages=["source"], settings=offline_settings)
    assert record.status == "success"
    assert set(record.counts) == {"jobs_sourced", "jobs_new", "sources_failed"}


def test_unknown_stage_recorded_as_error(offline_settings):
    record = run_daily.run_pipeline(stages=["does_not_exist"], settings=offline_settings)
    assert record.status == "failed"
    assert any("unknown stage" in e for e in record.errors)


def test_stage_failure_is_caught_and_skipped(monkeypatch, offline_settings):
    def boom(ctx: StageContext) -> StageResult:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(run_daily.REGISTRY, "source", boom)
    record = run_daily.run_pipeline(
        stages=["source", "log_and_digest"], settings=offline_settings
    )
    # One stage failed, one succeeded -> partial; the run still completed.
    assert record.status == "partial"
    assert any("kaboom" in e for e in record.errors)
    assert record.counts["new_jobs_today"] == 0


def test_degraded_stage_outcome_is_recorded_without_raising(monkeypatch, offline_settings):
    """A stage that returns normally but reports ok=False (e.g. every source
    failed without raising) must still surface as a run error — same as an
    exception would — instead of being invisible because nothing raised."""

    def degraded(ctx: StageContext) -> StageResult:
        return StageResult(name="source", counts={}, notes="every source failed", ok=False)

    monkeypatch.setitem(run_daily.REGISTRY, "source", degraded)
    record = run_daily.run_pipeline(
        stages=["source", "log_and_digest"], settings=offline_settings
    )
    assert record.status == "partial"
    assert any("every source failed" in e for e in record.errors)

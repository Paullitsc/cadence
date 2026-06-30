from __future__ import annotations

from internship_pipeline import run_daily
from internship_pipeline.models import StageContext, StageResult


def test_run_all_stub_stages_succeed():
    record = run_daily.run_pipeline()
    assert record.status == "success"
    assert record.errors == []
    assert record.finished_at is not None
    # Each stub contributes its zero-count key.
    assert record.counts["jobs_sourced"] == 0
    assert "digests_sent" in record.counts


def test_subset_runs_only_selected_stage():
    record = run_daily.run_pipeline(stages=["source"])
    assert record.status == "success"
    assert set(record.counts) == {"jobs_sourced"}


def test_unknown_stage_recorded_as_error():
    record = run_daily.run_pipeline(stages=["does_not_exist"])
    assert record.status == "failed"
    assert any("unknown stage" in e for e in record.errors)


def test_stage_failure_is_caught_and_skipped(monkeypatch):
    def boom(ctx: StageContext) -> StageResult:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(run_daily.REGISTRY, "source", boom)
    record = run_daily.run_pipeline(stages=["source", "log_and_digest"])
    # One stage failed, one succeeded -> partial; the run still completed.
    assert record.status == "partial"
    assert any("kaboom" in e for e in record.errors)
    assert record.counts["digests_sent"] == 0

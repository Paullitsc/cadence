"""End-to-end dry run: every stage from bundled fixtures, zero live credentials."""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.config import build_dry_run_settings
from internship_pipeline.run_daily import run_pipeline
from internship_pipeline.storage.sqlite_store import SQLiteStore


def test_dry_run_exercises_all_stages_offline(tmp_path):
    settings = build_dry_run_settings(work_dir=str(tmp_path))
    record = run_pipeline(settings=settings)

    assert record.status == "success" and record.errors == []
    c = record.counts
    assert c["jobs_new"] == 3                 # three fixture roles, all new
    assert c["applications_prepared"] == 3    # all pass the (relaxed) fit threshold
    # Both dual-trigger paths fire deterministically: one target-company role
    # ("Dry Run Labs") and one RECENT-posted role; the STALE role stays app-only.
    assert c["dual_trigger"] == 2
    assert c["outreach_drafted"] == 4         # 2 dual-trigger roles × (email + linkedin)
    assert c["digest_written"] == 1

    # Digest file written to the temp work dir.
    assert (Path(settings.digest_dir) / "latest.html").exists()

    # Rows actually persisted to the isolated temp SQLite tracker.
    store = SQLiteStore(settings.database_path)
    assert len(store.list_applications(status="pending_review")) == 3
    assert len(store.list_outreach(status="pending_review")) >= 2


def test_dry_run_is_hermetic_no_email_no_paid_lookups(tmp_path):
    settings = build_dry_run_settings(work_dir=str(tmp_path))
    record = run_pipeline(settings=settings)
    assert record.counts["digest_emailed"] == 0      # never emails in dry-run
    assert record.counts["paid_lookups_used"] == 0   # no billable contact lookups

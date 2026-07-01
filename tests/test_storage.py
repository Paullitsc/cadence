from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from internship_pipeline.models import Job, JobSource, RunRecord
from internship_pipeline.storage.sqlite_store import SQLiteStore


def _job(url: str, title: str = "Intern", company: str = "Acme") -> Job:
    return Job(
        company_name=company,
        title=title,
        url=url,
        locations=["Remote"],
        source_feed=JobSource.GREENHOUSE,
    )


def test_upsert_detects_new_vs_seen_by_hash(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))

    first = store.upsert_jobs([_job("https://x/1"), _job("https://x/2")])
    assert first.new_count == 2
    assert first.seen == 0

    # Re-run: one identical (note trailing slash -> same dedupe hash) + one new.
    second = store.upsert_jobs([_job("https://x/1/"), _job("https://x/3")])
    assert second.new_count == 1
    assert {j.url for j in second.new} == {"https://x/3"}
    assert second.seen == 1


def test_within_run_duplicates_inserted_once(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    result = store.upsert_jobs([_job("https://x/1"), _job("https://x/1")])
    assert result.new_count == 1


def test_existing_keys_and_persisted_columns(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    job = _job("https://x/1")
    store.upsert_jobs([job])
    assert store.existing_keys([job.dedupe_key(), "missing"]) == {job.dedupe_key()}

    with sqlite3.connect(str(tmp_path / "p.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["company_name"] == "Acme"
    assert json.loads(row["locations"]) == ["Remote"]
    assert row["source_feed"] == "greenhouse"
    assert row["first_seen_at"] and row["last_seen_at"]


def test_record_run_persists_row(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    run = RunRecord(
        run_id="run123",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        counts={"jobs_new": 3},
        errors=[],
        status="success",
    )
    store.record_run(run)
    with sqlite3.connect(str(tmp_path / "p.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", ("run123",)).fetchone()
    assert row["status"] == "success"
    assert json.loads(row["counts"]) == {"jobs_new": 3}


def test_empty_upsert_is_noop(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    result = store.upsert_jobs([])
    assert result.new_count == 0 and result.seen == 0

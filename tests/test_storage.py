from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

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


def test_stale_job_keys_only_returns_jobs_not_recently_seen(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    fresh = _job("https://x/fresh")
    stale = _job("https://x/stale")
    store.upsert_jobs([fresh, stale])

    # Backdate `stale`'s last_seen_at directly (upsert_jobs always stamps "now").
    with sqlite3.connect(str(tmp_path / "p.db")) as conn:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE dedupe_key = ?",
            ("2000-01-01T00:00:00+00:00", stale.dedupe_key()),
        )

    cutoff = "2020-01-01T00:00:00+00:00"
    assert store.stale_job_keys(cutoff) == {stale.dedupe_key()}


def test_conn_closes_the_connection_not_just_commits(tmp_path, monkeypatch):
    """_conn() must actually close each connection, not just commit/rollback
    and leave it open for the GC to eventually collect."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    store = SQLiteStore(str(tmp_path / "p.db"))
    store.upsert_jobs([_job("https://x/1")])

    assert opened  # at least the schema-init connection was tracked
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

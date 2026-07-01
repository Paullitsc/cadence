"""SQLite storage — the local-dev fallback backend.

Self-initializing (CREATE TABLE IF NOT EXISTS) and connection-per-operation, so
every call is self-contained and idempotent. Schema mirrors
``storage/sql/sqlite.sql`` (the committed migration).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from ..logging_config import get_logger
from ..models import Job, RunRecord
from .base import Storage, UpsertResult, chunked

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    dedupe_key    TEXT PRIMARY KEY,
    company_name  TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    locations     TEXT,            -- JSON array
    date_posted   TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    source        TEXT,
    source_feed   TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs (first_seen_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    counts      TEXT,              -- JSON object
    errors      TEXT               -- JSON array
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore(Storage):
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def existing_keys(self, keys: Iterable[str]) -> set[str]:
        key_list = list(keys)
        if not key_list:
            return set()
        found: set[str] = set()
        with self._conn() as conn:
            for chunk in chunked(key_list, 500):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT dedupe_key FROM jobs WHERE dedupe_key IN ({placeholders})",
                    chunk,
                )
                found.update(r[0] for r in rows)
        return found

    def upsert_jobs(self, jobs: list[Job]) -> UpsertResult:
        if not jobs:
            return UpsertResult()
        now = _now()
        existing = self.existing_keys(j.dedupe_key() for j in jobs)
        new: list[Job] = []
        with self._conn() as conn:
            for job in jobs:
                key = job.dedupe_key()
                if key in existing:
                    conn.execute(
                        "UPDATE jobs SET last_seen_at=?, active=?, title=?, locations=? "
                        "WHERE dedupe_key=?",
                        (now, int(job.active), job.title, json.dumps(job.locations), key),
                    )
                else:
                    conn.execute(
                        "INSERT INTO jobs (dedupe_key, company_name, title, url, locations, "
                        "date_posted, active, source, source_feed, first_seen_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            job.company_name,
                            job.title,
                            job.url,
                            json.dumps(job.locations),
                            job.date_posted,
                            int(job.active),
                            job.source,
                            job.source_feed.value if job.source_feed else None,
                            now,
                            now,
                        ),
                    )
                    new.append(job)
                    existing.add(key)  # guard against in-batch duplicates
        return UpsertResult(new=new, seen=len(jobs) - len(new))

    def record_run(self, run: RunRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, finished_at, status, "
                "counts, errors) VALUES (?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.status,
                    json.dumps(run.counts),
                    json.dumps(run.errors),
                ),
            )

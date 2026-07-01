-- SQLite schema (local-dev fallback) — Phase 1.
-- Applied automatically by SQLiteStore on first use; kept here as the committed
-- migration / reference. Mirrors storage/sql/postgres.sql.

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

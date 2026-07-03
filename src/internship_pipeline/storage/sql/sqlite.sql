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

-- Phase 2: prepared (never auto-submitted) applications, keyed by job dedupe_key.
CREATE TABLE IF NOT EXISTS applications (
    dedupe_key           TEXT PRIMARY KEY,   -- FK -> jobs.dedupe_key
    company_name         TEXT NOT NULL,
    title                TEXT NOT NULL,
    url                  TEXT NOT NULL,
    fit_score            REAL NOT NULL DEFAULT 0,
    keywords             TEXT,               -- JSON array
    tailored_resume_path TEXT,               -- rendered PDF path (nullable)
    tailored_resume_yaml TEXT,               -- RenderCV YAML (auditable)
    drafted_answers      TEXT,               -- JSON object: question -> answer
    human_review         INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'pending_review',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);

-- Phase 3: drafted (never auto-sent) cold-outreach messages, one row per (job, channel).
-- channel="linkedin" is DRAFT-ONLY (LinkedIn automation is a ban-risk red zone).
CREATE TABLE IF NOT EXISTS outreach (
    outreach_id          TEXT PRIMARY KEY,   -- make_outreach_id(dedupe_key, channel)
    dedupe_key           TEXT NOT NULL,      -- FK -> jobs.dedupe_key
    company_name         TEXT NOT NULL,
    title                TEXT NOT NULL,
    url                  TEXT NOT NULL,
    channel              TEXT NOT NULL,      -- email | linkedin
    contact_name         TEXT,
    contact_email        TEXT,
    contact_title        TEXT,
    contact_source       TEXT NOT NULL DEFAULT 'none',   -- hunter | apollo | pattern_guess | none
    contact_confidence   INTEGER,           -- 0-100 (NULL for a pattern guess)
    contact_verified     INTEGER NOT NULL DEFAULT 0,
    contact_note         TEXT,
    subject              TEXT,              -- email only
    body                 TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'pending_review',
    suppressed           INTEGER NOT NULL DEFAULT 0,
    human_review         INTEGER NOT NULL DEFAULT 0,
    used_llm             INTEGER NOT NULL DEFAULT 0,
    sent_at              TEXT,
    provider_message_id  TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach (status);
CREATE INDEX IF NOT EXISTS idx_outreach_dedupe ON outreach (dedupe_key);

-- Phase 3: do-not-contact list, enforced before any send. An entry is a full email
-- address or a bare domain (opt out an entire company).
CREATE TABLE IF NOT EXISTS suppressions (
    entry       TEXT PRIMARY KEY,   -- email address OR bare domain, lowercased
    reason      TEXT,
    created_at  TEXT NOT NULL
);

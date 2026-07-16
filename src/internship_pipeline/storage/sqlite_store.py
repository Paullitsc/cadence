"""SQLite storage — the local-dev fallback backend.

Self-initializing (CREATE TABLE IF NOT EXISTS) and connection-per-operation, so
every call is self-contained and idempotent. Schema mirrors
``storage/sql/sqlite.sql`` (the committed migration).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..logging_config import get_logger
from ..models import Application, CvCacheEntry, Job, Outreach, RunRecord
from ..networking.models import Person
from .base import Storage, UpsertResult, chunked, suppression_matches

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

CREATE TABLE IF NOT EXISTS applications (
    dedupe_key           TEXT PRIMARY KEY,   -- FK -> jobs.dedupe_key
    company_name         TEXT NOT NULL,
    title                TEXT NOT NULL,
    url                  TEXT NOT NULL,
    fit_score            REAL NOT NULL DEFAULT 0,
    keywords             TEXT,               -- JSON array
    tailored_resume_path TEXT,               -- rendered PDF path (nullable)
    tailored_resume_yaml TEXT,               -- cv-doc YAML (auditable)
    cv_drive_link        TEXT,               -- Phase 5: durable Drive link to the PDF
    drafted_answers      TEXT,               -- JSON object: question -> answer
    recommended_bullets  TEXT,               -- JSON array of {id, text} (AI recommendation)
    final_bullets        TEXT,               -- JSON array of {id, text} (human's selection)
    reviewed_at          TEXT,               -- when the human submitted the review
    human_review         INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'pending_review',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);

CREATE TABLE IF NOT EXISTS outreach (
    outreach_id          TEXT PRIMARY KEY,   -- make_outreach_id(dedupe_key, channel)
    dedupe_key           TEXT NOT NULL,      -- FK -> jobs.dedupe_key
    company_name         TEXT NOT NULL,
    title                TEXT NOT NULL,
    url                  TEXT NOT NULL,
    channel              TEXT NOT NULL,      -- email | linkedin (linkedin = draft-only)
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
    gmail_draft_id       TEXT,               -- Phase 5: real Gmail draft (edit + send)
    gmail_draft_link     TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach (status);
CREATE INDEX IF NOT EXISTS idx_outreach_dedupe ON outreach (dedupe_key);

CREATE TABLE IF NOT EXISTS suppressions (
    entry       TEXT PRIMARY KEY,   -- email address OR bare domain, lowercased
    reason      TEXT,
    created_at  TEXT NOT NULL
);

-- Phase 5: cross-run CV cache. cache_key = hash(selected bullet ids + keyword set);
-- a hit reuses the stored CV outright (no LLM tailoring call, no render, no upload).
CREATE TABLE IF NOT EXISTS cv_cache (
    cache_key            TEXT PRIMARY KEY,
    tailored_resume_yaml TEXT NOT NULL,
    cv_drive_link        TEXT,
    drive_file_id        TEXT,
    pdf_path             TEXT,
    recommended_bullets  TEXT,               -- JSON array of {id, text}
    created_at           TEXT NOT NULL
);

-- Phase 6: networking campaign people (one row per person per target company;
-- LinkedIn ladder state machine — see networking/models.py).
CREATE TABLE IF NOT EXISTS people (
    person_id         TEXT PRIMARY KEY,   -- make_person_id(campaign, company, n)
    campaign          TEXT NOT NULL DEFAULT 'default',
    company_name      TEXT NOT NULL,
    company_domain    TEXT,
    company_website   TEXT,
    company_linkedin  TEXT,
    company_blurb     TEXT NOT NULL DEFAULT '',
    tier              INTEGER NOT NULL DEFAULT 2,
    name              TEXT,
    role              TEXT,
    linkedin_url      TEXT,
    email             TEXT,
    status            TEXT NOT NULL DEFAULT 'queued',
    status_changed_at TEXT,               -- escalation timers measure from here
    draft_kind        TEXT,               -- connect | message (6b: email)
    draft_subject     TEXT,               -- email only (Phase 6b)
    draft_body        TEXT NOT NULL DEFAULT '',
    used_llm          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_people_status ON people (status);
"""

# Columns added after a table first shipped: applied with ALTER TABLE on startup so
# existing local databases pick them up (CREATE TABLE IF NOT EXISTS won't).
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("applications", "cv_drive_link", "TEXT"),
    ("applications", "recommended_bullets", "TEXT"),
    ("applications", "final_bullets", "TEXT"),
    ("applications", "reviewed_at", "TEXT"),
    ("outreach", "gmail_draft_id", "TEXT"),
    ("outreach", "gmail_draft_link", "TEXT"),
    ("cv_cache", "recommended_bullets", "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore(Storage):
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            for table, column, col_type in _MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection for one operation, committing/rolling back AND
        closing it when the ``with`` block exits.

        ``sqlite3.Connection`` is itself a context manager, but its
        ``__exit__`` only handles the transaction (commit on success, rollback
        on exception) — it does NOT close the connection. Every call site here
        does ``with self._conn() as conn:``, so without this wrapper every one
        of those connections would sit open until CPython's refcounting GC
        happens to collect it (not guaranteed on other implementations, and a
        real fd/lock leak in the meantime).
        """
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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

    def stale_job_keys(self, cutoff_iso: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT dedupe_key FROM jobs WHERE last_seen_at < ?", (cutoff_iso,)
            ).fetchall()
        return {r[0] for r in rows}

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

    def get_job(self, dedupe_key: str) -> Optional[Job]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        if row is None:
            return None
        return Job(
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            locations=json.loads(row["locations"] or "[]"),
            date_posted=row["date_posted"],
            active=bool(row["active"]),
            source=row["source"],
            source_feed=row["source_feed"],
        )

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

    def save_application(self, app: Application) -> None:
        now = _now()
        with self._conn() as conn:
            # Preserve created_at on update; refresh updated_at.
            row = conn.execute(
                "SELECT created_at FROM applications WHERE dedupe_key=?", (app.dedupe_key,)
            ).fetchone()
            created = row[0] if row else now
            conn.execute(
                "INSERT OR REPLACE INTO applications (dedupe_key, company_name, title, url, "
                "fit_score, keywords, tailored_resume_path, tailored_resume_yaml, "
                "cv_drive_link, drafted_answers, recommended_bullets, final_bullets, "
                "reviewed_at, human_review, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    app.dedupe_key,
                    app.company_name,
                    app.title,
                    app.url,
                    app.fit_score,
                    json.dumps(app.keywords),
                    app.tailored_resume_path,
                    app.tailored_resume_yaml,
                    app.cv_drive_link,
                    json.dumps(app.drafted_answers),
                    json.dumps(app.recommended_bullets),
                    json.dumps(app.final_bullets),
                    app.reviewed_at,
                    int(app.human_review),
                    app.status,
                    created,
                    now,
                ),
            )

    @staticmethod
    def _row_to_application(row: sqlite3.Row) -> Application:
        return Application(
            dedupe_key=row["dedupe_key"],
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            fit_score=row["fit_score"],
            keywords=json.loads(row["keywords"] or "[]"),
            tailored_resume_path=row["tailored_resume_path"],
            tailored_resume_yaml=row["tailored_resume_yaml"],
            cv_drive_link=row["cv_drive_link"],
            drafted_answers=json.loads(row["drafted_answers"] or "{}"),
            recommended_bullets=json.loads(row["recommended_bullets"] or "[]"),
            final_bullets=json.loads(row["final_bullets"] or "[]"),
            reviewed_at=row["reviewed_at"],
            human_review=bool(row["human_review"]),
            status=row["status"],
        )

    def get_application(self, dedupe_key: str) -> Optional[Application]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        return None if row is None else self._row_to_application(row)

    def list_applications(self, status: Optional[str] = None) -> list[Application]:
        with self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM applications ORDER BY fit_score DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM applications WHERE status=? ORDER BY fit_score DESC", (status,)
                ).fetchall()
        return [self._row_to_application(r) for r in rows]

    # --- Phase 3: outreach + suppression list ---

    def save_outreach(self, outreach: Outreach) -> None:
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM outreach WHERE outreach_id=?", (outreach.outreach_id,)
            ).fetchone()
            created = row[0] if row else now
            conn.execute(
                "INSERT OR REPLACE INTO outreach (outreach_id, dedupe_key, company_name, title, "
                "url, channel, contact_name, contact_email, contact_title, contact_source, "
                "contact_confidence, contact_verified, contact_note, subject, body, status, "
                "suppressed, human_review, used_llm, sent_at, provider_message_id, "
                "gmail_draft_id, gmail_draft_link, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outreach.outreach_id,
                    outreach.dedupe_key,
                    outreach.company_name,
                    outreach.title,
                    outreach.url,
                    outreach.channel,
                    outreach.contact_name,
                    outreach.contact_email,
                    outreach.contact_title,
                    outreach.contact_source,
                    outreach.contact_confidence,
                    int(outreach.contact_verified),
                    outreach.contact_note,
                    outreach.subject,
                    outreach.body,
                    outreach.status,
                    int(outreach.suppressed),
                    int(outreach.human_review),
                    int(outreach.used_llm),
                    outreach.sent_at,
                    outreach.provider_message_id,
                    outreach.gmail_draft_id,
                    outreach.gmail_draft_link,
                    created,
                    now,
                ),
            )

    @staticmethod
    def _row_to_outreach(row: sqlite3.Row) -> Outreach:
        return Outreach(
            outreach_id=row["outreach_id"],
            dedupe_key=row["dedupe_key"],
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            channel=row["channel"],
            contact_name=row["contact_name"],
            contact_email=row["contact_email"],
            contact_title=row["contact_title"],
            contact_source=row["contact_source"],
            contact_confidence=row["contact_confidence"],
            contact_verified=bool(row["contact_verified"]),
            contact_note=row["contact_note"],
            subject=row["subject"],
            body=row["body"] or "",
            status=row["status"],
            suppressed=bool(row["suppressed"]),
            human_review=bool(row["human_review"]),
            used_llm=bool(row["used_llm"]),
            sent_at=row["sent_at"],
            provider_message_id=row["provider_message_id"],
            gmail_draft_id=row["gmail_draft_id"],
            gmail_draft_link=row["gmail_draft_link"],
        )

    def get_outreach(self, outreach_id: str) -> Optional[Outreach]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM outreach WHERE outreach_id=?", (outreach_id,)
            ).fetchone()
        return None if row is None else self._row_to_outreach(row)

    def list_outreach(self, status: Optional[str] = None) -> list[Outreach]:
        with self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM outreach ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outreach WHERE status=? ORDER BY created_at DESC", (status,)
                ).fetchall()
        return [self._row_to_outreach(r) for r in rows]

    # --- Phase 5: cross-run CV cache ---

    def get_cv_cache(self, cache_key: str) -> Optional[CvCacheEntry]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cv_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_cv_cache(row)

    @staticmethod
    def _row_to_cv_cache(row: sqlite3.Row) -> CvCacheEntry:
        return CvCacheEntry(
            cache_key=row["cache_key"],
            tailored_resume_yaml=row["tailored_resume_yaml"],
            cv_drive_link=row["cv_drive_link"],
            drive_file_id=row["drive_file_id"],
            pdf_path=row["pdf_path"],
            recommended_bullets=json.loads(row["recommended_bullets"] or "[]"),
        )

    def list_cv_cache(self) -> list[CvCacheEntry]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM cv_cache ORDER BY created_at").fetchall()
        return [self._row_to_cv_cache(r) for r in rows]

    def save_cv_cache(self, entry: CvCacheEntry) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cv_cache (cache_key, tailored_resume_yaml, "
                "cv_drive_link, drive_file_id, pdf_path, recommended_bullets, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    entry.cache_key,
                    entry.tailored_resume_yaml,
                    entry.cv_drive_link,
                    entry.drive_file_id,
                    entry.pdf_path,
                    json.dumps(entry.recommended_bullets),
                    _now(),
                ),
            )

    # --- Phase 6: networking campaign people ---

    def save_person(self, person: Person) -> None:
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM people WHERE person_id=?", (person.person_id,)
            ).fetchone()
            created = row[0] if row else now
            conn.execute(
                "INSERT OR REPLACE INTO people (person_id, campaign, company_name, "
                "company_domain, company_website, company_linkedin, company_blurb, tier, "
                "name, role, linkedin_url, email, status, status_changed_at, draft_kind, "
                "draft_subject, draft_body, used_llm, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    person.person_id,
                    person.campaign,
                    person.company_name,
                    person.company_domain,
                    person.company_website,
                    person.company_linkedin,
                    person.company_blurb,
                    person.tier,
                    person.name,
                    person.role,
                    person.linkedin_url,
                    person.email,
                    person.status,
                    person.status_changed_at,
                    person.draft_kind,
                    person.draft_subject,
                    person.draft_body,
                    int(person.used_llm),
                    created,
                    now,
                ),
            )

    @staticmethod
    def _row_to_person(row: sqlite3.Row) -> Person:
        return Person(
            person_id=row["person_id"],
            campaign=row["campaign"],
            company_name=row["company_name"],
            company_domain=row["company_domain"],
            company_website=row["company_website"],
            company_linkedin=row["company_linkedin"],
            company_blurb=row["company_blurb"] or "",
            tier=row["tier"],
            name=row["name"],
            role=row["role"],
            linkedin_url=row["linkedin_url"],
            email=row["email"],
            status=row["status"],
            status_changed_at=row["status_changed_at"],
            draft_kind=row["draft_kind"],
            draft_subject=row["draft_subject"],
            draft_body=row["draft_body"] or "",
            used_llm=bool(row["used_llm"]),
        )

    def get_person(self, person_id: str) -> Optional[Person]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM people WHERE person_id=?", (person_id,)
            ).fetchone()
        return None if row is None else self._row_to_person(row)

    def list_people(self, status: Optional[str] = None) -> list[Person]:
        with self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM people ORDER BY tier, company_name, person_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM people WHERE status=? ORDER BY tier, company_name, person_id",
                    (status,),
                ).fetchall()
        return [self._row_to_person(r) for r in rows]

    def add_suppression(self, entry: str, reason: Optional[str] = None) -> None:
        normalized = (entry or "").strip().lower()
        if not normalized:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO suppressions (entry, reason, created_at) VALUES (?,?,?)",
                (normalized, reason, _now()),
            )

    def list_suppressions(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT entry FROM suppressions").fetchall()
        return [r[0] for r in rows]

    def is_suppressed(self, email: str) -> bool:
        return suppression_matches(email, self.list_suppressions())

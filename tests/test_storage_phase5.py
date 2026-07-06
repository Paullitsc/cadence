"""Phase 5 storage: new columns round-trip, cv_cache table, and the ALTER-TABLE
migration path for databases created before Phase 5."""

from __future__ import annotations

import sqlite3

from internship_pipeline.models import Application, CvCacheEntry, Outreach, make_outreach_id
from internship_pipeline.storage import SQLiteStore


def test_application_cv_drive_link_round_trip(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    app = Application(
        dedupe_key="k1", company_name="Acme", title="Intern", url="https://a/1",
        cv_drive_link="https://drive.google.com/file/d/F1/view",
    )
    store.save_application(app)
    assert store.get_application("k1").cv_drive_link == "https://drive.google.com/file/d/F1/view"


def test_outreach_gmail_draft_fields_round_trip(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    o = Outreach(
        outreach_id=make_outreach_id("k1", "email"), dedupe_key="k1", company_name="Acme",
        title="Intern", url="https://a/1", channel="email",
        status="gmail_draft_created", gmail_draft_id="d1",
        gmail_draft_link="https://mail.google.com/mail/u/0/#drafts?compose=m1",
    )
    store.save_outreach(o)
    stored = store.get_outreach(o.outreach_id)
    assert stored.gmail_draft_id == "d1"
    assert stored.status == "gmail_draft_created"
    assert store.list_outreach(status="gmail_draft_created") == [stored]


def test_cv_cache_round_trip_and_upsert(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    assert store.get_cv_cache("nope") is None
    entry = CvCacheEntry(
        cache_key="abc", tailored_resume_yaml="cv: {}", cv_drive_link=None, pdf_path="/x.pdf"
    )
    store.save_cv_cache(entry)
    assert store.get_cv_cache("abc").pdf_path == "/x.pdf"

    entry.cv_drive_link = "https://drive/f"  # a later run gains the durable link
    store.save_cv_cache(entry)
    assert store.get_cv_cache("abc").cv_drive_link == "https://drive/f"


def test_pre_phase5_database_is_migrated_in_place(tmp_path):
    """Opening a DB whose tables predate Phase 5 adds the new columns via ALTER."""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE applications (dedupe_key TEXT PRIMARY KEY, company_name TEXT NOT NULL, "
            "title TEXT NOT NULL, url TEXT NOT NULL, fit_score REAL NOT NULL DEFAULT 0, "
            "keywords TEXT, tailored_resume_path TEXT, tailored_resume_yaml TEXT, "
            "drafted_answers TEXT, human_review INTEGER NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL DEFAULT 'pending_review', created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO applications (dedupe_key, company_name, title, url, created_at, "
            "updated_at) VALUES ('k1', 'Acme', 'Intern', 'https://a/1', 't', 't')"
        )
    store = SQLiteStore(str(db))
    app = store.get_application("k1")  # would raise without the migration
    assert app.cv_drive_link is None
    app.cv_drive_link = "https://drive/f"
    store.save_application(app)
    assert store.get_application("k1").cv_drive_link == "https://drive/f"

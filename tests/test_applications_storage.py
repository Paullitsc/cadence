from __future__ import annotations

import sqlite3

from internship_pipeline.models import Application
from internship_pipeline.storage.sqlite_store import SQLiteStore


def _app(**over) -> Application:
    base = dict(
        dedupe_key="abc123",
        company_name="DataCorp",
        title="Backend Intern",
        url="https://x/1",
        fit_score=0.72,
        keywords=["python", "kafka"],
        tailored_resume_path="data/resumes/abc123.pdf",
        tailored_resume_yaml="cv:\n  name: Test\n",
        drafted_answers={"Why?": "Because backend."},
        human_review=True,
        status="pending_review",
    )
    base.update(over)
    return Application(**base)


def test_save_and_get_application_roundtrip(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    store.save_application(_app())

    got = store.get_application("abc123")
    assert got is not None
    assert got.company_name == "DataCorp"
    assert got.fit_score == 0.72
    assert got.keywords == ["python", "kafka"]
    assert got.drafted_answers == {"Why?": "Because backend."}
    assert got.human_review is True
    assert got.status == "pending_review"
    assert got.tailored_resume_path.endswith("abc123.pdf")


def test_get_missing_application_returns_none(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    assert store.get_application("nope") is None


def test_list_applications_orders_by_fit_and_filters_by_status(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    store.save_application(_app(dedupe_key="low", fit_score=0.30, status="pending_review"))
    store.save_application(_app(dedupe_key="high", fit_score=0.90, status="pending_review"))
    store.save_application(_app(dedupe_key="done", fit_score=0.99, status="submitted"))

    pending = store.list_applications(status="pending_review")
    assert [a.dedupe_key for a in pending] == ["high", "low"]  # highest fit first
    assert {a.dedupe_key for a in store.list_applications()} == {"low", "high", "done"}


def test_save_application_upsert_preserves_created_at(tmp_path):
    db = str(tmp_path / "p.db")
    store = SQLiteStore(db)
    store.save_application(_app(status="pending_review"))
    with sqlite3.connect(db) as conn:
        created_first = conn.execute(
            "SELECT created_at FROM applications WHERE dedupe_key=?", ("abc123",)
        ).fetchone()[0]

    # Re-save (e.g. prepare_applications adds answers) — created_at must be stable.
    store.save_application(_app(drafted_answers={"Q": "A"}))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT created_at, updated_at FROM applications WHERE dedupe_key=?", ("abc123",)
        ).fetchone()
    assert row[0] == created_first
    assert store.get_application("abc123").drafted_answers == {"Q": "A"}

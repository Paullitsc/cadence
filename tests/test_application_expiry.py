"""Application lifecycle: pending_review -> expired when the job goes stale.

This is storage-only — it must never touch the tracker sheet's Status column
(human-owned after the initial "prepared" write; see tracker/rows.py). The
digest stage runs it before computing the pending queue so a freshly-expired
application isn't shown as still awaiting review the same run it expires.
"""

from __future__ import annotations

import sqlite3

from internship_pipeline.config import Settings
from internship_pipeline.models import Application, Job, StageContext
from internship_pipeline.stages import log_and_digest
from internship_pipeline.storage.sqlite_store import SQLiteStore


def _settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        digest_dir=str(tmp_path / "digests"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _seed_stale_pending_application(db_path: str) -> str:
    store = SQLiteStore(db_path)
    job = Job(company_name="Acme", title="Intern", url="https://x/1")
    store.upsert_jobs([job])
    store.save_application(
        Application(
            dedupe_key=job.dedupe_key(), company_name="Acme", title="Intern",
            url="https://x/1", status="pending_review",
        )
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE dedupe_key = ?",
            ("2000-01-01T00:00:00+00:00", job.dedupe_key()),
        )
    return job.dedupe_key()


def test_stale_pending_application_expires_and_drops_from_pending_count(tmp_path):
    settings = _settings(tmp_path, application_expiry_days=21)
    dedupe_key = _seed_stale_pending_application(settings.database_path)

    ctx = StageContext(run_id="r1", settings=settings)
    result = log_and_digest.run(ctx)

    assert result.counts["applications_expired"] == 1
    assert result.counts["applications_pending"] == 0

    store = SQLiteStore(settings.database_path)
    app = store.get_application(dedupe_key)
    assert app.status == "expired"


def test_expiry_disabled_when_zero(tmp_path):
    settings = _settings(tmp_path, application_expiry_days=0)
    dedupe_key = _seed_stale_pending_application(settings.database_path)

    ctx = StageContext(run_id="r1", settings=settings)
    result = log_and_digest.run(ctx)

    assert result.counts["applications_expired"] == 0
    assert result.counts["applications_pending"] == 1

    store = SQLiteStore(settings.database_path)
    app = store.get_application(dedupe_key)
    assert app.status == "pending_review"


def test_recently_seen_application_does_not_expire(tmp_path):
    settings = _settings(tmp_path, application_expiry_days=21)
    store = SQLiteStore(settings.database_path)
    job = Job(company_name="Acme", title="Intern", url="https://x/fresh")
    store.upsert_jobs([job])  # last_seen_at = now
    store.save_application(
        Application(
            dedupe_key=job.dedupe_key(), company_name="Acme", title="Intern",
            url="https://x/fresh", status="pending_review",
        )
    )

    ctx = StageContext(run_id="r1", settings=settings)
    result = log_and_digest.run(ctx)

    assert result.counts["applications_expired"] == 0
    assert result.counts["applications_pending"] == 1

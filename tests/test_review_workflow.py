"""Review-workflow plumbing: the new storage fields round-trip, the tracker sync
is gated on reviewed applications, and the Status dropdown is re-applied to an
existing sheet."""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.models import Application, CvCacheEntry, Job, StageContext
from internship_pipeline.stages.sync_tracker import _collect_applications
from internship_pipeline.storage import SQLiteStore
from internship_pipeline.tracker.sheets import (
    ANSWERS_TAB,
    APPLICATIONS_TAB,
    ensure_tracker_tabs,
)


# --- storage round-trips ----------------------------------------------------------
def test_application_review_fields_round_trip(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    app = Application(
        dedupe_key="k1", company_name="Acme", title="Intern", url="https://a/1",
        recommended_bullets=[{"id": "e0b0", "text": "tailored text"}],
        final_bullets=[{"id": "e0b0", "text": "final text"}],
        reviewed_at="2026-07-08T12:00:00+00:00",
        status="reviewed",
    )
    store.save_application(app)
    stored = store.get_application("k1")
    assert stored.recommended_bullets == [{"id": "e0b0", "text": "tailored text"}]
    assert stored.final_bullets == [{"id": "e0b0", "text": "final text"}]
    assert stored.reviewed_at == "2026-07-08T12:00:00+00:00"
    assert store.list_applications(status="reviewed") == [stored]


def test_cv_cache_recommendation_round_trip(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    store.save_cv_cache(CvCacheEntry(
        cache_key="c1", tailored_resume_yaml="cv: {}",
        recommended_bullets=[{"id": "p0b0", "text": "kept"}],
    ))
    assert store.get_cv_cache("c1").recommended_bullets == [{"id": "p0b0", "text": "kept"}]
    assert store.list_cv_cache()[0].recommended_bullets == [{"id": "p0b0", "text": "kept"}]


def test_get_job_round_trip(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    job = Job(company_name="Acme", title="Intern", url="https://a/1",
              locations=["Remote", "Boston, MA"])
    store.upsert_jobs([job])
    stored = store.get_job(job.dedupe_key())
    assert stored.locations == ["Remote", "Boston, MA"]
    assert store.get_job("missing") is None


# --- sync gating -------------------------------------------------------------------
def test_sync_collects_only_reviewed_applications(tmp_path):
    settings = Settings(
        _env_file=None, storage_backend="sqlite", database_path=str(tmp_path / "t.db"),
    )
    ctx = StageContext(run_id="t", settings=settings)
    storage = ctx.get_storage()

    job = Job(company_name="Acme", title="Intern", url="https://a/reviewed",
              locations=["Montreal, QC"])
    storage.upsert_jobs([job])
    reviewed = Application(
        dedupe_key=job.dedupe_key(), company_name="Acme", title="Intern",
        url=job.url, status="reviewed", reviewed_at="2026-07-08T00:00:00+00:00",
        cv_drive_link="https://drive/reviewed",
    )
    pending = Application(
        dedupe_key="pending1", company_name="Beta", title="Intern", url="https://a/pending",
        status="pending_review", cv_drive_link="https://drive/pending",
    )
    storage.save_application(reviewed)
    storage.save_application(pending)

    apps, locations, cv_links = _collect_applications(ctx)
    # only the reviewed application syncs; the pending one waits for the human
    assert [a.dedupe_key for a in apps] == [job.dedupe_key()]
    assert locations[job.dedupe_key()] == ["Montreal, QC"]
    # ...but EVERY stored Drive link is known, for "same as row N" rendering
    assert cv_links == {job.dedupe_key(): "https://drive/reviewed",
                        "pending1": "https://drive/pending"}
    storage.close()


# --- Status dropdown re-applied to existing tabs ------------------------------------
class _Exec:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class FakeSheets:
    """Just enough of the Sheets discovery client for ensure_tracker_tabs."""

    def __init__(self, tabs: dict[str, int]):
        self.tabs = tabs
        self.batch_bodies: list[dict] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, fields=None, range=None):  # noqa: A002 (API name)
        return _Exec({
            "sheets": [{"properties": {"title": t, "sheetId": i}}
                       for t, i in self.tabs.items()],
            "values": [],
        })

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802 (API name)
        self.batch_bodies.append(body)
        return _Exec({"replies": [{"addSheet": {"properties": {"sheetId": 99}}}]})

    def update(self, **kwargs):
        return _Exec({})

    def append(self, **kwargs):
        return _Exec({})


def test_existing_applications_tab_gets_dropdown_reapplied():
    fake = FakeSheets({APPLICATIONS_TAB: 7, ANSWERS_TAB: 8})
    tab_ids = ensure_tracker_tabs(fake, "sheet-id")
    assert tab_ids == {APPLICATIONS_TAB: 7, ANSWERS_TAB: 8}

    validations = [
        req for body in fake.batch_bodies for req in body["requests"]
        if "setDataValidation" in req
    ]
    assert len(validations) == 1  # existing tab → dropdown refreshed, nothing else
    rule = validations[0]["setDataValidation"]
    assert rule["range"]["sheetId"] == 7
    values = [v["userEnteredValue"] for v in rule["rule"]["condition"]["values"]]
    assert "prepared" in values and "offer" in values


def test_fresh_tabs_created_with_dropdown_once():
    fake = FakeSheets({})
    ensure_tracker_tabs(fake, "sheet-id")
    validations = [
        req for body in fake.batch_bodies for req in body["requests"]
        if "setDataValidation" in req
    ]
    assert len(validations) == 1  # creation setup includes it; no double-apply

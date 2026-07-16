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
    """Just enough of the Sheets discovery client for ensure_tracker_tabs and the
    shared sync: per-tab values, and deleteDimension requests actually remove rows
    so a post-deletion re-read sees the shifted sheet (like the real API)."""

    def __init__(self, tabs: dict[str, int], values_by_tab: dict[str, list[list[str]]] | None = None):
        self.tabs = tabs
        self.values_by_tab = values_by_tab or {}
        self.batch_bodies: list[dict] = []
        self.appended: list[dict] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def _tab_for_range(self, range):  # noqa: A002 (API name)
        return (range or "").split("!")[0].strip("'")

    def get(self, spreadsheetId, fields=None, range=None):  # noqa: A002 (API name)
        return _Exec({
            "sheets": [{"properties": {"title": t, "sheetId": i}}
                       for t, i in self.tabs.items()],
            "values": self.values_by_tab.get(self._tab_for_range(range), []),
        })

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802 (API name)
        self.batch_bodies.append(body)
        for req in body.get("requests", []):
            rng = req.get("deleteDimension", {}).get("range")
            if rng and rng.get("dimension") == "ROWS":
                tab = next(t for t, i in self.tabs.items() if i == rng["sheetId"])
                del self.values_by_tab[tab][rng["startIndex"] : rng["endIndex"]]
        return _Exec({"replies": [{"addSheet": {"properties": {"sheetId": 99}}}]})

    def update(self, **kwargs):
        return _Exec({})

    def append(self, **kwargs):
        self.appended.append(kwargs)
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


# --- human rejection/withdrawal via the Status dropdown ------------------------------
def test_rejected_and_withdrawn_statuses_remove_rows_and_mark_storage(tmp_path):
    from internship_pipeline.tracker.auth import TrackerServices
    from internship_pipeline.tracker.rows import ANSWERS_HEADERS, COL_KEY, COL_STATUS, HEADERS
    from internship_pipeline.tracker.sync import sync_applications_to_sheet

    store = SQLiteStore(str(tmp_path / "t.db"))
    rejected = Application(
        dedupe_key="rej1", company_name="Acme", title="Intern", url="https://a/rej",
        status="reviewed", reviewed_at="2026-07-13T00:00:00+00:00",
    )
    withdrawn = Application(
        dedupe_key="wit1", company_name="Gamma", title="Intern", url="https://a/wit",
        status="reviewed", reviewed_at="2026-07-13T00:00:00+00:00",
    )
    fresh = Application(
        dedupe_key="new1", company_name="Beta", title="Intern", url="https://a/new",
        status="reviewed", reviewed_at="2026-07-13T00:00:00+00:00",
    )
    store.save_application(rejected)
    store.save_application(withdrawn)
    store.save_application(fresh)

    rejected_row = [""] * len(HEADERS)
    rejected_row[COL_STATUS] = "rejected"  # the human picked it in the dropdown
    rejected_row[COL_KEY] = "rej1"
    withdrawn_row = [""] * len(HEADERS)
    withdrawn_row[COL_STATUS] = "withdrawn"  # the human pulled out of this one
    withdrawn_row[COL_KEY] = "wit1"
    fake = FakeSheets(
        {APPLICATIONS_TAB: 7, ANSWERS_TAB: 8},
        values_by_tab={
            APPLICATIONS_TAB: [list(HEADERS), rejected_row, withdrawn_row],
            ANSWERS_TAB: [list(ANSWERS_HEADERS)],
        },
    )

    outcome = sync_applications_to_sheet(
        TrackerServices(sheets=fake, drive=None), "sheet-id",
        [rejected, withdrawn, fresh], storage=store,
    )

    # Both rows are gone from the sheet, each recorded under its own status.
    assert outcome.rows_removed == 2
    assert fake.values_by_tab[APPLICATIONS_TAB] == [list(HEADERS)]
    assert store.get_application("rej1").status == "rejected"
    assert store.get_application("wit1").status == "withdrawn"
    # The discarded applications were dropped from the upsert; only the fresh one
    # was appended — nothing resurrects the deleted rows.
    assert outcome.rows_appended == 1
    (appended,) = fake.appended
    assert 'https://a/new' in str(appended["body"]["values"])
    assert 'rej1' not in str(appended["body"]["values"])
    assert 'wit1' not in str(appended["body"]["values"])
    store.close()


def test_sync_with_empty_batch_still_processes_rejections(tmp_path):
    """The daily reconcile must honor rejections even when nothing new is reviewed."""
    from internship_pipeline.tracker.auth import TrackerServices
    from internship_pipeline.tracker.rows import ANSWERS_HEADERS, COL_KEY, COL_STATUS, HEADERS
    from internship_pipeline.tracker.sync import sync_applications_to_sheet

    store = SQLiteStore(str(tmp_path / "t.db"))
    store.save_application(Application(
        dedupe_key="rej1", company_name="Acme", title="Intern", url="https://a/rej",
        status="reviewed",
    ))

    rejected_row = [""] * len(HEADERS)
    rejected_row[COL_STATUS] = "rejected"
    rejected_row[COL_KEY] = "rej1"
    fake = FakeSheets(
        {APPLICATIONS_TAB: 7, ANSWERS_TAB: 8},
        values_by_tab={
            APPLICATIONS_TAB: [list(HEADERS), rejected_row],
            ANSWERS_TAB: [list(ANSWERS_HEADERS)],
        },
    )

    outcome = sync_applications_to_sheet(
        TrackerServices(sheets=fake, drive=None), "sheet-id", [], storage=store,
    )
    assert outcome.rows_removed == 1
    assert outcome.rows_appended == 0
    assert store.get_application("rej1").status == "rejected"
    store.close()

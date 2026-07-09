"""Review app: recommendation prechecking, selection assembly, and the submit
flow (storage update + review gating), all offline — no HTTP server, no Google,
no LaTeX engine (conftest hides any real one)."""

from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.models import Application
from internship_pipeline.resume.loader import load_master_resume
from internship_pipeline.review.app import ReviewApp
from internship_pipeline.review.selection import entry_options, selection_to_bullets
from internship_pipeline.storage import get_storage

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


@pytest.fixture
def resume():
    return load_master_resume(FIXTURE)


def _app(**overrides) -> Application:
    base = dict(
        dedupe_key="k1",
        company_name="DataCorp",
        title="Backend Intern",
        url="https://x/job",
        fit_score=0.7,
        keywords=["python", "kafka"],
        recommended_bullets=[
            {"id": "e0b0", "text": "Built a **Kafka** data pipeline (tailored)."},
            {"id": "p0b0", "text": "Implemented a SQL query planner in Java for a teaching database."},
        ],
    )
    base.update(overrides)
    return Application(**base)


# --- entry_options ---------------------------------------------------------------
def test_entry_options_precheck_recommended_bullets(resume):
    entries = entry_options(resume, _app())
    assert [e.source for e in entries] == ["experience", "project"]
    exp, proj = entries
    assert exp.title == "Software Engineer Intern — Acme Labs"

    by_id = {b.id: b for e in entries for b in e.bullets}
    assert by_id["e0b0"].recommended and by_id["p0b0"].recommended
    assert not by_id["e0b1"].recommended
    # a recommended bullet shows its TAILORED text; others show the master text
    assert by_id["e0b0"].text == "Built a **Kafka** data pipeline (tailored)."
    assert by_id["e0b1"].text.startswith("Added integration tests")


def test_entry_options_fallback_matches_yaml_for_legacy_apps(resume):
    # An application from before recommended_bullets existed: only the CV YAML.
    legacy_yaml = (
        "cv:\n  sections:\n    experience:\n"
        "      - company: Acme Labs\n        highlights:\n"
        "          - 'Added **integration tests** covering the ingestion service in Python.'\n"
    )
    app = _app(recommended_bullets=[], tailored_resume_yaml=legacy_yaml)
    entries = entry_options(resume, app)
    by_id = {b.id: b for e in entries for b in e.bullets}
    assert by_id["e0b1"].recommended  # matched by normalized text (bold stripped)
    assert not by_id["e0b0"].recommended


# --- selection_to_bullets ----------------------------------------------------------
def test_selection_orders_recommended_first_then_added(resume):
    app = _app()
    # Human keeps both recommendations and adds e0b1; checkbox order is arbitrary.
    bullets = selection_to_bullets(resume, app, ["e0b1", "p0b0", "e0b0"])
    assert [tb.ref.id for tb in bullets] == ["e0b0", "p0b0", "e0b1"]
    # recommended keep tailored text; the added one gets deterministic keyword bolding
    assert bullets[0].text == "Built a **Kafka** data pipeline (tailored)."
    assert "**Python**" in bullets[2].text


def test_selection_ignores_unknown_and_duplicate_ids(resume):
    bullets = selection_to_bullets(resume, _app(), ["e0b0", "e0b0", "ghost", "p0b0"])
    assert [tb.ref.id for tb in bullets] == ["e0b0", "p0b0"]


# --- ReviewApp.submit ---------------------------------------------------------------
@pytest.fixture
def review_app(tmp_path, resume):
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        master_resume_file=FIXTURE,
        resume_output_dir=str(tmp_path / "resumes"),
    )
    storage = get_storage(settings)
    yield ReviewApp(settings, storage, resume)
    storage.close()


def test_submit_finalizes_and_marks_reviewed(review_app):
    review_app.storage.save_application(_app(status="pending_review"))

    result = review_app.submit("k1", ["e0b0", "e0b1"])
    assert result.get("ok") is True
    assert result["sheet_synced"] is False  # tracker unconfigured → daily run syncs

    stored = review_app.storage.get_application("k1")
    assert stored.status == "reviewed"
    assert stored.reviewed_at
    assert [b["id"] for b in stored.final_bullets] == ["e0b0", "e0b1"]
    # the final artifact reflects the human's selection, not the recommendation
    assert "integration tests" in stored.tailored_resume_yaml
    assert "query planner" not in stored.tailored_resume_yaml.lower()
    assert Path(stored.tailored_resume_path).exists()


def test_submit_rejects_unknown_app_and_empty_selection(review_app):
    assert "error" in review_app.submit("nope", ["e0b0"])
    review_app.storage.save_application(_app(status="pending_review"))
    assert "error" in review_app.submit("k1", [])


def test_preview_renders_selection_and_reports_no_engine(review_app):
    review_app.storage.save_application(_app(status="pending_review"))
    data = review_app.preview("k1", ["e0b0"])
    assert data["bullets"] == 1
    assert data["pdf"] is False and data["engine"] is None  # no engine in tests


def test_pages_render_html(review_app):
    review_app.storage.save_application(_app(status="pending_review"))
    index = review_app.index_html()
    assert "DataCorp" in index and "1 pending" in index
    page = review_app.review_html("k1")
    assert "Backend Intern" in page and "AI pick" in page
    assert review_app.review_html("missing") is None

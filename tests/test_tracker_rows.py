"""Phase 5: pure sheet-row building + upsert diffing (no network, no Google libs)."""

from __future__ import annotations

from internship_pipeline.models import Application
from internship_pipeline.tracker.rows import (
    ANSWERS_HEADERS,
    COL_ANSWERS,
    COL_CV,
    COL_DATE,
    COL_FIT,
    COL_KEY,
    COL_KEYWORDS,
    COL_LOCATIONS,
    COL_NOTES,
    COL_STATUS,
    COL_TITLE,
    HEADERS,
    plan_answers_upsert,
    plan_applications_upsert,
    spreadsheet_url,
)


def _app(key: str, **over) -> Application:
    defaults = dict(
        dedupe_key=key,
        company_name="Acme",
        title="Backend Intern",
        url=f"https://acme.dev/jobs/{key}",
        fit_score=0.8215,
        keywords=["python", "aws"],
        cv_drive_link=None,
    )
    defaults.update(over)
    return Application(**defaults)


def test_new_application_appends_full_row():
    plan = plan_applications_upsert(
        [HEADERS], [_app("k1", cv_drive_link="https://drive/f1")],
        prepared_date="2026-07-06", locations_by_key={"k1": ["NYC", "Remote"]},
    )
    assert len(plan.appends) == 1 and not plan.updates
    row = plan.appends[0]
    assert row[COL_TITLE] == '=HYPERLINK("https://acme.dev/jobs/k1", "Backend Intern")'
    assert row[COL_DATE] == "2026-07-06"
    assert row[COL_LOCATIONS] == "NYC · Remote"
    assert row[COL_STATUS] == "prepared"  # the pipeline only ever writes the first status
    assert row[COL_NOTES] == ""
    assert row[COL_CV] == '=HYPERLINK("https://drive/f1", "CV")'
    assert row[COL_FIT] == "0.82"
    assert row[COL_KEYWORDS] == "python, aws"
    assert row[COL_KEY] == "k1"


def test_existing_row_human_cells_never_touched_blank_cells_filled():
    existing_row = [""] * len(HEADERS)
    existing_row[COL_TITLE] = "Backend Intern"
    existing_row[COL_STATUS] = "interviewing"  # human moved it
    existing_row[COL_NOTES] = "spoke with Rae"  # human-owned
    existing_row[COL_KEY] = "k1"
    plan = plan_applications_upsert(
        [HEADERS, existing_row],
        [_app("k1", cv_drive_link="https://drive/f1")],
        prepared_date="2026-07-06",
    )
    assert not plan.appends
    touched = {(u.row, u.col) for u in plan.updates}
    assert (2, COL_STATUS) not in touched  # never overwrite the human's status
    assert (2, COL_NOTES) not in touched  # Notes is never written
    assert (2, COL_TITLE) not in touched  # non-blank cells stay as they are
    cv = next(u for u in plan.updates if u.col == COL_CV)
    assert cv.row == 2 and "https://drive/f1" in cv.value


def test_grouped_jobs_share_cv_as_same_as_row():
    shared = "https://drive/shared"
    plan = plan_applications_upsert(
        [HEADERS],
        [_app("k1", cv_drive_link=shared), _app("k2", cv_drive_link=shared)],
        prepared_date="2026-07-06",
    )
    assert plan.appends[0][COL_CV] == f'=HYPERLINK("{shared}", "CV")'
    assert plan.appends[1][COL_CV] == "same as row 2"  # row 1 is the header


def test_new_row_references_existing_rows_cv():
    shared = "https://drive/shared"
    existing_row = [""] * len(HEADERS)
    existing_row[COL_CV] = "CV"  # display value of the existing hyperlink
    existing_row[COL_KEY] = "k1"
    plan = plan_applications_upsert(
        [HEADERS, existing_row],
        [_app("k1", cv_drive_link=shared), _app("k2", cv_drive_link=shared)],
        prepared_date="2026-07-06",
    )
    assert len(plan.appends) == 1
    assert plan.appends[0][COL_CV] == "same as row 2"


def test_rerun_with_no_changes_is_a_no_op():
    plan1 = plan_applications_upsert([HEADERS], [_app("k1")], prepared_date="2026-07-06")
    snapshot = [HEADERS, plan1.appends[0]]
    plan2 = plan_applications_upsert(snapshot, [_app("k1")], prepared_date="2026-07-07")
    assert plan2.empty


def test_answers_plan_appends_only_missing_pairs_and_anchors():
    app = _app("k1", drafted_answers={"Why us?": "Because …", "Biggest challenge?": "The …"})
    plan, anchors = plan_answers_upsert([ANSWERS_HEADERS], [app])
    assert len(plan.appends) == 2 and not plan.updates
    assert plan.appends[0] == ["k1", "Acme", "Backend Intern", "Why us?", "Because …", ""]
    assert anchors["k1"] == 2  # first data row

    # Re-plan against the applied snapshot: nothing new, anchors stable — the
    # human's "Your edited answer" column is never part of any update.
    snapshot = [ANSWERS_HEADERS, *plan.appends]
    snapshot[1][5] = "my hand-edited answer"
    plan2, anchors2 = plan_answers_upsert(snapshot, [app])
    assert plan2.empty
    assert anchors2["k1"] == 2


def test_applications_row_links_to_answers_tab():
    app = _app("k1", drafted_answers={"Why us?": "Because …"})
    plan = plan_applications_upsert(
        [HEADERS], [app], prepared_date="2026-07-06",
        answers_gid=77, answers_rows={"k1": 2},
    )
    assert plan.appends[0][COL_ANSWERS] == '=HYPERLINK("#gid=77&range=A2", "1 answer")'


def test_title_quotes_are_escaped_in_formula():
    app = _app("k1", title='The "Best" Internship')
    plan = plan_applications_upsert([HEADERS], [app], prepared_date="2026-07-06")
    assert '""Best""' in plan.appends[0][COL_TITLE]


def test_spreadsheet_url():
    assert spreadsheet_url("SID") == "https://docs.google.com/spreadsheets/d/SID"

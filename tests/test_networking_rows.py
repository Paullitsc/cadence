"""Networking tab planning: appends, cell ownership, two-way status merge, and
closed-row removal — all pure, against sheet snapshots as lists of strings."""

from __future__ import annotations

from internship_pipeline.networking.models import Person
from internship_pipeline.networking.rows import (
    COL_DRAFT,
    COL_KEY,
    COL_LINKEDIN,
    COL_NEXT_STEP,
    COL_PERSON,
    COL_STATUS,
    HEADERS,
    apply_sheet_edits,
    next_step,
    parse_sheet_people,
    plan_closed_removals,
    plan_people_upsert,
)

NOW_ISO = "2026-07-16T13:00:00+00:00"


def person(pid: str, **kw) -> Person:
    defaults = dict(company_name="Robotics Co", tier=1)
    defaults.update(kw)
    return Person(person_id=pid, **defaults)


def _row(pid: str, *, status: str = "queued", name: str = "", role: str = "",
         linkedin: str = "", draft: str = "", notes: str = "") -> list[str]:
    row = [""] * len(HEADERS)
    row[0] = "Robotics Co"
    row[1] = "1"
    row[COL_PERSON] = name
    row[3] = role
    row[COL_LINKEDIN] = linkedin
    row[COL_STATUS] = status
    row[COL_DRAFT] = draft
    row[8] = notes
    row[COL_KEY] = pid
    return row


def _plan(existing, people):
    return plan_people_upsert(existing, people, accept_window_days=10, reply_window_days=7)


def test_unknown_person_appends_full_row():
    p = person("x-1", name="Jane Doe", linkedin_url="https://li/in/jane",
               company_linkedin="https://li/co/robotics", status="connect_drafted",
               draft_body="Hi Jane — note")
    plan = _plan([HEADERS], [p])
    assert len(plan.appends) == 1 and not plan.updates
    row = plan.appends[0]
    assert row[COL_KEY] == "x-1"
    assert row[COL_STATUS] == "connect_drafted"
    assert row[COL_DRAFT] == "Hi Jane — note"
    assert row[COL_LINKEDIN] == "https://li/in/jane"  # plain URL, not a formula
    assert row[0].startswith("=HYPERLINK(")  # company links to its LinkedIn page


def test_existing_row_fills_blanks_and_refreshes_pipeline_cells_only():
    existing = [HEADERS, _row("x-1", status="queued", notes="my note")]
    p = person("x-1", name="Jane Doe", status="connect_drafted", draft_body="the note")
    plan = _plan(existing, [p])
    assert not plan.appends
    updated_cols = {(u.row, u.col) for u in plan.updates}
    assert (2, COL_PERSON) in updated_cols  # blank identity cell filled
    assert (2, COL_STATUS) in updated_cols  # pipeline-owned refresh
    assert (2, COL_DRAFT) in updated_cols
    assert (2, COL_NEXT_STEP) in updated_cols
    # Notes column is never touched.
    assert all(u.col != 8 for u in plan.updates)


def test_existing_nonblank_identity_cells_are_never_overwritten():
    existing = [HEADERS, _row("x-1", status="queued", name="Human Choice")]
    p = person("x-1", name="Yaml Name")
    plan = _plan(existing, [p])
    assert all(u.col != COL_PERSON for u in plan.updates)


def test_matching_sheet_needs_no_updates():
    p = person("x-1", name="Jane", status="connect_sent",
               status_changed_at=NOW_ISO, draft_body="note text")
    fresh_next = next_step(p, accept_window_days=10, reply_window_days=7)
    existing = [HEADERS, _row("x-1", status="connect_sent", name="Jane", draft="note text")]
    existing[1][COL_NEXT_STEP] = fresh_next
    plan = _plan(existing, [p])
    assert plan.empty


def test_closed_people_removed_and_never_planned():
    existing = [HEADERS, _row("x-1"), _row("x-2"), _row("x-3")]
    people = [person("x-1", status="closed"), person("x-2"), person("x-3", status="closed")]
    closed = {p.person_id for p in people if p.status == "closed"}
    assert plan_closed_removals(existing, closed) == [2, 4]
    plan = _plan([HEADERS, _row("x-2")], people)
    assert not plan.appends  # x-1/x-3 closed → not re-appended


def test_parse_and_merge_absorbs_human_edits():
    existing = [
        HEADERS,
        _row("x-1", status="connect_sent", name="Jane Doe", linkedin="https://li/in/jane"),
        _row("x-2", status="queued"),  # human typed an invalid target below
        [""] * len(HEADERS),  # hand-added row without an id → ignored
    ]
    existing[2][COL_STATUS] = "message_drafted"  # pipeline-owned value, not human-settable
    edits = parse_sheet_people(existing)
    assert set(edits) == {"x-1", "x-2"}

    p1 = person("x-1", status="connect_drafted", status_changed_at="2026-07-01T00:00:00+00:00")
    p2 = person("x-2", status="queued")
    changed = apply_sheet_edits([p1, p2], edits, now_iso=NOW_ISO)

    assert p1 in changed
    assert p1.status == "connect_sent"  # valid forward move absorbed
    assert p1.status_changed_at == NOW_ISO  # timer restamped from the human's action
    assert p1.name == "Jane Doe" and p1.linkedin_url == "https://li/in/jane"

    assert p2 not in changed
    assert p2.status == "queued"  # invalid edit ignored (and later rewritten on the sheet)


def test_merge_blank_cells_never_clear_stored_values():
    existing = [HEADERS, _row("x-1", status="connect_sent")]  # Person cell blank
    p = person("x-1", name="Stored Name", status="connect_sent")
    changed = apply_sheet_edits([p], parse_sheet_people(existing), now_iso=NOW_ISO)
    assert changed == []
    assert p.name == "Stored Name"

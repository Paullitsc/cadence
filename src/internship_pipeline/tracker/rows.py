"""Pure sheet-row building + upsert diffing for the Applications/Answers tabs.

No network, no Google imports — everything here takes plain values (the sheet
snapshot as lists of strings, ``Application`` models) and returns a ``SheetPlan``
of appends + cell updates for the API wrapper to apply. Fixture-testable.

Upsert contract (assignment A1): rows are keyed by the hidden dedupe-key column.
Re-runs must never overwrite what the human owns — **Notes is never written after
the initial insert, Status is written once as "prepared" and then only by the
human** — and for every other (pipeline-owned) cell the safe rule is: after the
initial row insert, only fill cells that are still blank. The CV cell is the one
exception: it always reflects the latest render, so re-reviewing an application
(e.g. after a renderer change) overwrites a stale Drive link instead of leaving it.

Two Status values act back on the sheet: a row the human sets to ``rejected`` or
``withdrawn`` is DELETED on the next sync (``plan_status_removals``), after the
stored application is marked with that same status so no later sync resurrects it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Application

# Applications tab layout. "Dedupe key" is last so it can be hidden without gaps.
HEADERS: list[str] = [
    "Title",
    "Date",
    "Company",
    "Location(s)",
    "Status",
    "Notes",
    "CV",
    "Answers",
    "Fit",
    "Keywords",
    "Dedupe key",
]
COL_TITLE, COL_DATE, COL_COMPANY, COL_LOCATIONS, COL_STATUS, COL_NOTES, COL_CV, \
    COL_ANSWERS, COL_FIT, COL_KEYWORDS, COL_KEY = range(len(HEADERS))

# The status workflow is the human's; the pipeline only ever writes the first value.
# "rejected" and "withdrawn" are special: the next sync REMOVES that row from the
# sheet (and marks the stored application with the same status so it never comes
# back) — it's how the human discards a role from the tracker, whether the company
# said no or the human pulled out. The full history stays in storage.
STATUS_OPTIONS: list[str] = [
    "prepared",
    "submitted",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
]

# The human-set Status values that remove a row from the sheet on the next sync.
REMOVAL_STATUSES: frozenset[str] = frozenset({"rejected", "withdrawn"})

# Columns the pipeline may fill on an EXISTING row — only when the cell is blank.
# Status is included (blank = a row that predates the dropdown); Notes never is.
_FILL_IF_BLANK: tuple[int, ...] = (
    COL_TITLE, COL_DATE, COL_COMPANY, COL_LOCATIONS, COL_STATUS,
    COL_ANSWERS, COL_FIT, COL_KEYWORDS,
)

# The CV cell always tracks the latest render, even on an existing non-blank row —
# unlike the other pipeline-owned columns, re-reviewing an application is expected
# to replace its Drive link (e.g. a renderer change re-uploads a new PDF).
_ALWAYS_REFRESH: tuple[int, ...] = (COL_CV,)

ANSWERS_HEADERS: list[str] = [
    "Dedupe key",
    "Company",
    "Title",
    "Question",
    "Drafted answer",
    "Your edited answer",
]


def spreadsheet_url(spreadsheet_id: str) -> str:
    """The canonical URL of the tracker spreadsheet (for the digest header)."""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"


def _formula_str(text: str) -> str:
    """Escape a string literal for inside a Sheets formula (quotes are doubled)."""
    return (text or "").replace('"', '""')


def hyperlink(url: str, label: str) -> str:
    """A clickable ``=HYPERLINK`` cell (written with valueInputOption=USER_ENTERED)."""
    return f'=HYPERLINK("{_formula_str(url)}", "{_formula_str(label)}")'


def answers_cell(answers_gid: int, anchor_row: int, count: int) -> str:
    """Applications-tab Answers cell: an in-spreadsheet link to the Answers tab.

    Uses the ``#gid=<id>&range=<A1>`` fragment form HYPERLINK accepts for links
    within the same spreadsheet.
    """
    label = f"{count} answer{'s' if count != 1 else ''}"
    return hyperlink(f"#gid={answers_gid}&range=A{anchor_row}", label)


@dataclass
class CellUpdate:
    """One cell write: ``row`` is the 1-based sheet row, ``col`` the 0-based column."""

    row: int
    col: int
    value: str


@dataclass
class SheetPlan:
    """The diff to apply: whole-row appends + blank-cell fills on existing rows."""

    appends: list[list[str]] = field(default_factory=list)
    updates: list[CellUpdate] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.appends and not self.updates


def _cell(row: list[str], col: int) -> str:
    return (row[col] if col < len(row) else "").strip()


def _cv_value(
    app: Application, row_number: int, link_to_row: dict[str, int]
) -> str:
    """The CV cell: a Drive link, or ``same as row N`` when a grouped job shares it."""
    link = (app.cv_drive_link or "").strip()
    if not link:
        return ""
    first_row = link_to_row.setdefault(link, row_number)
    if first_row != row_number:
        return f"same as row {first_row}"
    return hyperlink(link, "CV")


def _build_row(
    app: Application,
    *,
    prepared_date: str,
    cv_value: str,
    answers_value: str,
) -> list[str]:
    row = [""] * len(HEADERS)
    row[COL_TITLE] = hyperlink(app.url, app.title)
    row[COL_DATE] = prepared_date
    row[COL_COMPANY] = app.company_name
    row[COL_STATUS] = STATUS_OPTIONS[0]  # "prepared" — every later transition is the human's
    row[COL_CV] = cv_value
    row[COL_ANSWERS] = answers_value
    row[COL_FIT] = f"{app.fit_score:.2f}"
    row[COL_KEYWORDS] = ", ".join(app.keywords[:10])
    row[COL_KEY] = app.dedupe_key
    return row


def plan_status_removals(existing: list[list[str]]) -> tuple[list[int], list[tuple[str, str]]]:
    """Rows whose Status the human set to ``rejected`` or ``withdrawn`` — to
    delete from the sheet.

    Returns (1-based sheet row numbers, ``(dedupe key, status)`` pairs), sheet
    order — the status rides along so the sync records the right one in storage.
    Rows without a dedupe key are left alone (a hand-added row is the human's, and
    without the key the removal couldn't be recorded in storage anyway).
    """
    row_numbers: list[int] = []
    removals: list[tuple[str, str]] = []
    for idx, row in enumerate(existing[1:], start=2):  # sheet rows start at 1; skip header
        key = _cell(row, COL_KEY)
        status = _cell(row, COL_STATUS).lower()
        if key and status in REMOVAL_STATUSES:
            row_numbers.append(idx)
            removals.append((key, status))
    return row_numbers, removals


def plan_applications_upsert(
    existing: list[list[str]],
    apps: list[Application],
    *,
    prepared_date: str,
    locations_by_key: dict[str, list[str]] | None = None,
    answers_gid: int | None = None,
    answers_rows: dict[str, int] | None = None,
    cv_links_by_key: dict[str, str] | None = None,
) -> SheetPlan:
    """Diff ``apps`` against the current sheet snapshot (``existing`` includes the
    header row; sheet row numbers are therefore ``index + 1``).

    * unknown dedupe key → append a full row (Status starts at "prepared");
    * known dedupe key → fill only BLANK pipeline-owned cells (Notes untouched,
      a human-set Status untouched), EXCEPT the CV cell, which always overwrites
      to the latest Drive link;
    * two apps sharing one ``cv_drive_link`` → the later row reads ``same as row N``.

    ``locations_by_key`` supplies job locations (the Application row doesn't carry
    them; the sync stage joins them in from the jobs it has on hand).
    ``cv_links_by_key`` supplies Drive links for sheet rows whose app is NOT in this
    sync batch (e.g. the human already moved its Status past pending) — without it a
    new job reusing an older row's CV would repeat the link instead of saying
    ``same as row N``.
    """
    locations_by_key = locations_by_key or {}
    answers_rows = answers_rows or {}
    cv_links_by_key = cv_links_by_key or {}
    by_key = {a.dedupe_key: a for a in apps}

    key_to_row: dict[str, int] = {}
    for idx, row in enumerate(existing[1:], start=2):  # sheet rows start at 1; skip header
        key = _cell(row, COL_KEY)
        if key:
            key_to_row.setdefault(key, idx)

    # Where each Drive link already lives, so grouped jobs can say "same as row N".
    # Sheet order wins: the first (topmost) row holding a link is the one later
    # rows point at.
    link_to_row: dict[str, int] = {}
    for key, row_number in key_to_row.items():
        app = by_key.get(key)
        link = (app.cv_drive_link if app else cv_links_by_key.get(key)) or ""
        if link.strip():
            link_to_row.setdefault(link.strip(), row_number)

    plan = SheetPlan()
    next_row = len(existing) + 1  # first appended row lands here

    for app in apps:
        key = app.dedupe_key
        answers_value = ""
        if app.drafted_answers and answers_gid is not None and key in answers_rows:
            answers_value = answers_cell(answers_gid, answers_rows[key], len(app.drafted_answers))
        locations = " · ".join(locations_by_key.get(key, []))

        row_number = key_to_row.get(key)
        if row_number is None:
            cv_value = _cv_value(app, next_row, link_to_row)
            row = _build_row(
                app, prepared_date=prepared_date, cv_value=cv_value, answers_value=answers_value
            )
            row[COL_LOCATIONS] = locations
            plan.appends.append(row)
            key_to_row[key] = next_row
            next_row += 1
            continue

        current = existing[row_number - 1]
        fresh = _build_row(
            app,
            prepared_date=prepared_date,
            cv_value=_cv_value(app, row_number, link_to_row),
            answers_value=answers_value,
        )
        fresh[COL_LOCATIONS] = locations
        for col in _FILL_IF_BLANK:
            if _cell(current, col) == "" and fresh[col]:
                plan.updates.append(CellUpdate(row=row_number, col=col, value=fresh[col]))
        for col in _ALWAYS_REFRESH:
            if fresh[col] and fresh[col] != _cell(current, col):
                plan.updates.append(CellUpdate(row=row_number, col=col, value=fresh[col]))
    return plan


def plan_answers_upsert(
    existing: list[list[str]],
    apps: list[Application],
) -> tuple[SheetPlan, dict[str, int]]:
    """Diff drafted answers against the Answers tab (append-only).

    One row per (dedupe key, question). Existing rows are never rewritten — the
    "Your edited answer" column is the human's. Returns the plan plus each key's
    anchor row (its first question row, after the plan is applied) so the
    Applications tab can link to it.
    """
    seen: set[tuple[str, str]] = set()
    anchors: dict[str, int] = {}
    for idx, row in enumerate(existing[1:], start=2):
        key, question = _cell(row, 0), _cell(row, 3)
        if key:
            anchors.setdefault(key, idx)
            if question:
                seen.add((key, question))

    plan = SheetPlan()
    next_row = len(existing) + 1
    for app in apps:
        for question, answer in app.drafted_answers.items():
            pair = (app.dedupe_key, question.strip())
            if not pair[1] or pair in seen:
                continue
            seen.add(pair)
            plan.appends.append(
                [app.dedupe_key, app.company_name, app.title, question, answer, ""]
            )
            anchors.setdefault(app.dedupe_key, next_row)
            next_row += 1
    return plan, anchors

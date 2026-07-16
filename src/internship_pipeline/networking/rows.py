"""Pure sheet-row building, parsing and diffing for the Networking tab.

Same contract style as ``tracker/rows.py`` (no network, no Google imports,
fixture-testable), but this tab is TWO-WAY: the human works the LinkedIn ladder
by editing cells, and the pipeline absorbs those edits before it writes.

Cell ownership:

* **Person / Role / LinkedIn** — the human's (they identify who to contact);
  the pipeline fills them only while blank (e.g. from the targets file) and
  reads non-blank values back into storage.
* **Status** — shared: the human sets the send/accept/reply events (validated
  forward-only in ``models.allowed_human_transition``), the pipeline sets the
  drafted/escalation states. After edits are absorbed, storage is authoritative
  and the cell is rewritten to it — so an invalid edit visibly reverts.
* **Next step / Draft to send** — pipeline-owned, always refreshed.
* **Notes** — human-owned, never written.
* A ``closed`` row is deleted from the sheet on the next sync (storage first,
  same as the tracker's ``rejected`` flow); ``replied`` rows stay as wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..tracker.rows import CellUpdate, SheetPlan, hyperlink
from .models import (
    STATUS_ACCEPTED,
    STATUS_CLOSED,
    STATUS_CONNECT_DRAFTED,
    STATUS_CONNECT_SENT,
    STATUS_EMAIL_DUE,
    STATUS_MESSAGE_DRAFTED,
    STATUS_MESSAGE_SENT,
    STATUS_ORDER,
    STATUS_QUEUED,
    STATUS_REPLIED,
    Person,
    allowed_human_transition,
)

HEADERS: list[str] = [
    "Company",
    "Tier",
    "Person",
    "Role",
    "LinkedIn",
    "Status",
    "Next step",
    "Draft to send",
    "Notes",
    "Person id",
]
COL_COMPANY, COL_TIER, COL_PERSON, COL_ROLE, COL_LINKEDIN, COL_STATUS, \
    COL_NEXT_STEP, COL_DRAFT, COL_NOTES, COL_KEY = range(len(HEADERS))

STATUS_OPTIONS: list[str] = list(STATUS_ORDER)

# Human-identity columns: filled while blank, read back when the human edits them.
_FILL_IF_BLANK: tuple[int, ...] = (COL_COMPANY, COL_TIER, COL_PERSON, COL_ROLE, COL_LINKEDIN)
# Pipeline-owned columns rewritten whenever storage disagrees with the sheet.
_ALWAYS_REFRESH: tuple[int, ...] = (COL_STATUS, COL_NEXT_STEP, COL_DRAFT)


def _cell(row: list[str], col: int) -> str:
    return (row[col] if col < len(row) else "").strip()


def next_step(person: Person, *, accept_window_days: int, reply_window_days: int) -> str:
    """The one-line instruction shown next to the row (pipeline-owned)."""
    status = person.status
    if status == STATUS_QUEUED:
        if not person.has_identity():
            return "You: pick a person — fill Person + LinkedIn (company page linked)."
        return "Queued — a connect note will be drafted within the daily budget."
    if status == STATUS_CONNECT_DRAFTED:
        return "You: send a connection request with the note → set Status to connect_sent."
    if status == STATUS_CONNECT_SENT:
        return f"Waiting for the accept — escalates {_due_date(person, accept_window_days)}."
    if status == STATUS_ACCEPTED:
        return "Accepted! A follow-up message will be drafted on the next run."
    if status == STATUS_MESSAGE_DRAFTED:
        return "You: send the message on LinkedIn → set Status to message_sent."
    if status == STATUS_MESSAGE_SENT:
        return f"Waiting for a reply — escalates {_due_date(person, reply_window_days)}."
    if status == STATUS_EMAIL_DUE:
        return "LinkedIn went quiet — email step lands in Phase 6b; nudge manually or close."
    if status == STATUS_REPLIED:
        return "In conversation — keep it going in your LinkedIn inbox."
    return ""


def _due_date(person: Person, window_days: int) -> str:
    if not person.status_changed_at:
        return f"after {window_days} days"
    try:
        changed = datetime.fromisoformat(person.status_changed_at)
    except ValueError:
        return f"after {window_days} days"
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    return (changed + timedelta(days=window_days)).strftime("%Y-%m-%d")


def _build_row(person: Person, *, accept_window_days: int, reply_window_days: int) -> list[str]:
    row = [""] * len(HEADERS)
    company = person.company_name
    link = (person.company_linkedin or person.company_website or "").strip()
    row[COL_COMPANY] = hyperlink(link, company) if link else company
    row[COL_TIER] = str(person.tier)
    row[COL_PERSON] = person.name or ""
    row[COL_ROLE] = person.role or ""
    # Plain URL (not a formula): the human edits this cell, and reading a formula
    # cell back returns its display text, which would break the read-back.
    row[COL_LINKEDIN] = person.linkedin_url or ""
    row[COL_STATUS] = person.status
    row[COL_NEXT_STEP] = next_step(
        person, accept_window_days=accept_window_days, reply_window_days=reply_window_days
    )
    row[COL_DRAFT] = person.draft_body or ""
    row[COL_KEY] = person.person_id
    return row


@dataclass
class SheetEdit:
    """What the human currently has in one row's editable cells."""

    person_id: str
    status: str
    name: str
    role: str
    linkedin_url: str


def parse_sheet_people(existing: list[list[str]]) -> dict[str, SheetEdit]:
    """The human-editable cell values, keyed by the hidden person id.

    Rows without an id are left alone (a hand-added row is the human's own).
    """
    edits: dict[str, SheetEdit] = {}
    for row in existing[1:]:  # skip header
        key = _cell(row, COL_KEY)
        if not key:
            continue
        edits.setdefault(
            key,
            SheetEdit(
                person_id=key,
                status=_cell(row, COL_STATUS).lower(),
                name=_cell(row, COL_PERSON),
                role=_cell(row, COL_ROLE),
                linkedin_url=_cell(row, COL_LINKEDIN),
            ),
        )
    return edits


def apply_sheet_edits(
    people: list[Person], edits: dict[str, SheetEdit], *, now_iso: str
) -> list[Person]:
    """Absorb the human's sheet edits into the given people (mutated in place).

    Identity cells win whenever non-blank and different (blank never clears a
    stored value — an accidental cell wipe must not erase state). Status moves
    are validated by ``allowed_human_transition``; a valid move also restamps
    ``status_changed_at`` so the escalation timers measure from the human's
    action. Returns the people that changed.
    """
    changed: list[Person] = []
    for person in people:
        edit = edits.get(person.person_id)
        if edit is None:
            continue
        dirty = False
        for attr, value in (("name", edit.name), ("role", edit.role), ("linkedin_url", edit.linkedin_url)):
            if value and value != (getattr(person, attr) or ""):
                setattr(person, attr, value)
                dirty = True
        if edit.status and edit.status != person.status:
            if allowed_human_transition(person.status, edit.status):
                person.status = edit.status
                person.status_changed_at = now_iso
                dirty = True
            # An invalid edit is silently reverted: the upsert plan rewrites the
            # Status cell back to storage's value.
        if dirty:
            changed.append(person)
    return changed


def plan_closed_removals(
    existing: list[list[str]], closed_ids: set[str]
) -> list[int]:
    """1-based sheet rows whose person is ``closed`` in storage — to delete."""
    return [
        idx
        for idx, row in enumerate(existing[1:], start=2)
        if _cell(row, COL_KEY) in closed_ids
    ]


def plan_people_upsert(
    existing: list[list[str]],
    people: list[Person],
    *,
    accept_window_days: int,
    reply_window_days: int,
) -> SheetPlan:
    """Diff ``people`` (post-merge storage state) against the sheet snapshot.

    Unknown person id → append a full row; known id → fill blank identity cells
    and rewrite the pipeline-owned cells wherever storage disagrees. ``closed``
    people are never planned (their rows are removed separately). ``existing``
    includes the header row, so sheet row numbers are ``index + 1``.
    """
    key_to_row: dict[str, int] = {}
    for idx, row in enumerate(existing[1:], start=2):
        key = _cell(row, COL_KEY)
        if key:
            key_to_row.setdefault(key, idx)

    plan = SheetPlan()
    for person in people:
        if person.status == STATUS_CLOSED:
            continue
        fresh = _build_row(
            person, accept_window_days=accept_window_days, reply_window_days=reply_window_days
        )
        row_number = key_to_row.get(person.person_id)
        if row_number is None:
            plan.appends.append(fresh)
            continue
        current = existing[row_number - 1]
        for col in _FILL_IF_BLANK:
            if _cell(current, col) == "" and fresh[col]:
                plan.updates.append(CellUpdate(row=row_number, col=col, value=fresh[col]))
        for col in _ALWAYS_REFRESH:
            # Compare stripped so whitespace normalization by Sheets doesn't cause
            # a rewrite of every draft cell on every sync.
            if fresh[col].strip() != _cell(current, col):
                plan.updates.append(CellUpdate(row=row_number, col=col, value=fresh[col]))
    return plan

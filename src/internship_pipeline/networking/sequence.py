"""Pure sequencing logic for the networking ladder: what is due today.

No I/O, no drafting, no storage — takes ``Person`` rows plus the clock/config
and returns typed decisions for the stage to execute. Fixture-testable.

Two kinds of decisions:

* ``plan_due`` — pipeline moves: which queued people get a connect note drafted
  (top-up to the daily budget, tier 1 first), which accepted people get their
  follow-up message drafted (always), and which sent-and-silent threads have
  aged past their window and escalate to ``email_due``.
* ``outstanding_actions`` — the human's current to-do list (drafted-but-unsent
  artifacts and stalled threads), rendered into the digest each morning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import (
    STATUS_ACCEPTED,
    STATUS_CONNECT_DRAFTED,
    STATUS_CONNECT_SENT,
    STATUS_EMAIL_DUE,
    STATUS_MESSAGE_DRAFTED,
    STATUS_MESSAGE_SENT,
    STATUS_QUEUED,
    Person,
)

DRAFT_CONNECT = "draft_connect"
DRAFT_MESSAGE = "draft_message"
MARK_EMAIL_DUE = "mark_email_due"


@dataclass
class DueAction:
    """One pipeline move to make this run."""

    person: Person
    action: str  # DRAFT_CONNECT | DRAFT_MESSAGE | MARK_EMAIL_DUE


@dataclass
class HumanAction:
    """One item on the human's to-do list (for the digest)."""

    person: Person
    kind: str  # "connect" | "message" | "stalled"
    instruction: str


def _tier_order(people: list[Person]) -> list[Person]:
    return sorted(people, key=lambda p: (p.tier, p.company_name.lower(), p.person_id))


def _aged_out(person: Person, *, now: datetime, window_days: int) -> bool:
    """True when ``status_changed_at`` is older than the window. A missing or
    unparseable timestamp never escalates (better a stuck row on the sheet than
    a spurious escalation)."""
    if not person.status_changed_at:
        return False
    try:
        changed = datetime.fromisoformat(person.status_changed_at)
    except ValueError:
        return False
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    return now - changed > timedelta(days=window_days)


def plan_due(
    people: list[Person],
    *,
    now: datetime,
    daily_connect_budget: int,
    accept_window_days: int,
    reply_window_days: int,
) -> list[DueAction]:
    """Everything the pipeline should do this run, in execution order.

    The connect budget is a TOP-UP: it counts notes already drafted and waiting
    (``connect_drafted``), so an unattended pipeline holds at most
    ``daily_connect_budget`` unsent connect notes instead of piling up five more
    every day. Tier 1 companies are drafted first.
    """
    due: list[DueAction] = []

    # Escalations first — they never compete with the budget.
    for person in people:
        if person.status == STATUS_CONNECT_SENT and _aged_out(
            person, now=now, window_days=accept_window_days
        ):
            due.append(DueAction(person, MARK_EMAIL_DUE))
        elif person.status == STATUS_MESSAGE_SENT and _aged_out(
            person, now=now, window_days=reply_window_days
        ):
            due.append(DueAction(person, MARK_EMAIL_DUE))

    # Accepted connects always get their message drafted — an accept is the
    # scarce event the whole ladder exists for.
    for person in _tier_order([p for p in people if p.status == STATUS_ACCEPTED]):
        due.append(DueAction(person, DRAFT_MESSAGE))

    outstanding = sum(1 for p in people if p.status == STATUS_CONNECT_DRAFTED)
    budget = max(0, daily_connect_budget - outstanding)
    ready = _tier_order(
        [p for p in people if p.status == STATUS_QUEUED and p.has_identity()]
    )
    due.extend(DueAction(person, DRAFT_CONNECT) for person in ready[:budget])
    return due


def outstanding_actions(people: list[Person]) -> list[HumanAction]:
    """The human's current LinkedIn to-dos, most actionable first."""
    actions: list[HumanAction] = []
    for person in _tier_order([p for p in people if p.status == STATUS_MESSAGE_DRAFTED]):
        actions.append(
            HumanAction(
                person,
                "message",
                "They accepted — send the drafted message on LinkedIn, then set "
                "Status to message_sent.",
            )
        )
    for person in _tier_order([p for p in people if p.status == STATUS_CONNECT_DRAFTED]):
        actions.append(
            HumanAction(
                person,
                "connect",
                "Send a connection request with the drafted note, then set Status "
                "to connect_sent.",
            )
        )
    for person in _tier_order([p for p in people if p.status == STATUS_EMAIL_DUE]):
        actions.append(
            HumanAction(
                person,
                "stalled",
                "LinkedIn went quiet — the email step lands in Phase 6b; nudge "
                "manually or set Status to closed.",
            )
        )
    return actions


def awaiting_person_count(people: list[Person]) -> int:
    """Companies still waiting for the human to pick someone to contact."""
    return sum(1 for p in people if p.status == STATUS_QUEUED and not p.has_identity())

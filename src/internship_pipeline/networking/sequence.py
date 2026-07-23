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

from ..outreach.contacts import guess_email_pattern
from .models import (
    STATUS_ACCEPTED,
    STATUS_CONNECT_DRAFTED,
    STATUS_CONNECT_SENT,
    STATUS_EMAIL_DRAFTED,
    STATUS_EMAIL_DUE,
    STATUS_MESSAGE_DRAFTED,
    STATUS_MESSAGE_SENT,
    STATUS_QUEUED,
    Person,
)

DRAFT_CONNECT = "draft_connect"
DRAFT_MESSAGE = "draft_message"
DRAFT_EMAIL = "draft_email"  # Phase 6b: draft the escalation email for an email_due person
MARK_EMAIL_DUE = "mark_email_due"


@dataclass
class DueAction:
    """One pipeline move to make this run."""

    person: Person
    action: str  # DRAFT_CONNECT | DRAFT_MESSAGE | DRAFT_EMAIL | MARK_EMAIL_DUE


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
    email_escalation_enabled: bool = False,
) -> list[DueAction]:
    """Everything the pipeline should do this run, in execution order.

    The connect budget is a TOP-UP: it counts notes already drafted and waiting
    (``connect_drafted``), so an unattended pipeline holds at most
    ``daily_connect_budget`` unsent connect notes instead of piling up five more
    every day. Tier 1 companies are drafted first.

    With ``email_escalation_enabled`` (Phase 6b), a person the timers have already
    parked at ``email_due`` gets a cold escalation email drafted — one per run, so
    the row moves ``email_due -> email_drafted`` and then waits on the human. When
    the flag is off, ``email_due`` stays a terminal placeholder (6a behavior).
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

    # Phase 6b: draft the escalation email for people already parked at email_due
    # (a person marked email_due THIS run is drafted next run — one move per run).
    # Not budget-limited: an escalation only fires after a full connect/message
    # cycle already stalled, so the volume is self-throttling.
    if email_escalation_enabled:
        for person in _tier_order(
            [p for p in people if p.status == STATUS_EMAIL_DUE and p.has_identity()]
        ):
            due.append(DueAction(person, DRAFT_EMAIL))

    outstanding = sum(1 for p in people if p.status == STATUS_CONNECT_DRAFTED)
    budget = max(0, daily_connect_budget - outstanding)
    ready = _tier_order(
        [p for p in people if p.status == STATUS_QUEUED and p.has_identity()]
    )
    due.extend(DueAction(person, DRAFT_CONNECT) for person in ready[:budget])
    return due


def _first_last(name: str | None) -> tuple[str | None, str | None]:
    parts = (name or "").strip().split()
    if not parts:
        return None, None
    return parts[0], (parts[-1] if len(parts) > 1 else None)


def _email_hint(person: Person) -> str:
    """A best-guess recipient address (or pattern) to show for a not-yet-verified
    escalation email — always a GUESS, surfaced so the human can complete it."""
    first, last = _first_last(person.name)
    contact = guess_email_pattern(
        person.company_name, domain=person.company_domain, first=first, last=last
    )
    return contact.email or contact.pattern or ""


def outstanding_actions(
    people: list[Person], *, email_escalation_enabled: bool = False
) -> list[HumanAction]:
    """The human's current to-dos, most actionable first.

    LinkedIn steps (connect/message) are copied out by hand; the Phase-6b email
    step, once ``email_escalation_enabled``, becomes a real "review + send" action
    (a Gmail draft link when the address was verified, otherwise the drafted copy
    plus a best-guess address to complete). With the flag off, ``email_due`` stays
    the 6a placeholder.
    """
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
    # Phase 6b: escalation emails awaiting the human.
    for person in _tier_order([p for p in people if p.status == STATUS_EMAIL_DRAFTED]):
        if person.gmail_draft_id:
            instruction = (
                "LinkedIn stalled — an escalation email is waiting in your Gmail "
                "drafts. Review, then send it and set Status to email_sent."
            )
        else:
            hint = _email_hint(person)
            to = f" (best guess: {hint})" if hint else ""
            instruction = (
                "LinkedIn stalled — an escalation email is drafted below. Find the "
                f"recipient's address{to}, send it, then set Status to email_sent."
            )
        actions.append(HumanAction(person, "email", instruction))
    for person in _tier_order([p for p in people if p.status == STATUS_EMAIL_DUE]):
        instruction = (
            "LinkedIn went quiet — an escalation email will be drafted on the next run."
            if email_escalation_enabled
            else "LinkedIn went quiet — nudge manually or set Status to closed."
        )
        actions.append(HumanAction(person, "stalled", instruction))
    return actions


def awaiting_person_count(people: list[Person]) -> int:
    """Companies still waiting for the human to pick someone to contact."""
    return sum(1 for p in people if p.status == STATUS_QUEUED and not p.has_identity())

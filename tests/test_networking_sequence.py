"""Sequencing: budget top-up, tier priority, escalation timers, to-do list."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from internship_pipeline.networking.models import Person
from internship_pipeline.networking.sequence import (
    DRAFT_CONNECT,
    DRAFT_MESSAGE,
    MARK_EMAIL_DUE,
    awaiting_person_count,
    outstanding_actions,
    plan_due,
)

NOW = datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)


def person(pid: str, *, status: str = "queued", tier: int = 2, name: str | None = "A Person",
           changed_days_ago: int | None = None, company: str | None = None) -> Person:
    changed = None
    if changed_days_ago is not None:
        changed = (NOW - timedelta(days=changed_days_ago)).isoformat()
    return Person(
        person_id=pid,
        company_name=company or pid,
        tier=tier,
        name=name,
        status=status,
        status_changed_at=changed,
    )


def _plan(people, budget=5):
    return plan_due(
        people, now=NOW, daily_connect_budget=budget,
        accept_window_days=10, reply_window_days=7,
    )


def test_connect_budget_tops_up_and_prefers_tier_one():
    people = [
        person("c-t3", tier=3),
        person("c-t1", tier=1),
        person("c-t2", tier=2),
        person("already-drafted", status="connect_drafted"),
        person("no-identity", name=None),  # queued but nobody picked yet
    ]
    due = _plan(people, budget=2)
    connects = [a.person.person_id for a in due if a.action == DRAFT_CONNECT]
    # One slot is already used by the outstanding drafted note → top-up drafts 1,
    # and the tier-1 company wins it.
    assert connects == ["c-t1"]


def test_budget_exhausted_by_outstanding_drafts():
    people = [person(f"waiting-{i}", status="connect_drafted") for i in range(3)]
    people.append(person("ready", tier=1))
    assert _plan(people, budget=3) == []


def test_accepted_always_gets_a_message_regardless_of_budget():
    people = [person("acc", status="accepted")] + [
        person(f"waiting-{i}", status="connect_drafted") for i in range(5)
    ]
    due = _plan(people, budget=0)
    assert [(a.person.person_id, a.action) for a in due] == [("acc", DRAFT_MESSAGE)]


def test_escalation_timers():
    people = [
        person("stale-connect", status="connect_sent", changed_days_ago=11),
        person("fresh-connect", status="connect_sent", changed_days_ago=3),
        person("stale-message", status="message_sent", changed_days_ago=8),
        person("fresh-message", status="message_sent", changed_days_ago=6),
        person("no-timestamp", status="connect_sent", changed_days_ago=None),
    ]
    due = _plan(people)
    escalated = {a.person.person_id for a in due if a.action == MARK_EMAIL_DUE}
    assert escalated == {"stale-connect", "stale-message"}


def test_outstanding_actions_orders_messages_first():
    people = [
        person("note-ready", status="connect_drafted"),
        person("msg-ready", status="message_drafted"),
        person("stalled", status="email_due"),
        person("quiet", status="connect_sent"),
    ]
    actions = outstanding_actions(people)
    assert [a.kind for a in actions] == ["message", "connect", "stalled"]
    assert all(a.instruction for a in actions)


def test_awaiting_person_count():
    people = [
        person("blank", name=None),
        person("named"),
        person("blank-but-done", name=None, status="closed"),
    ]
    assert awaiting_person_count(people) == 1

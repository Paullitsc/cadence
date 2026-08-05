"""Phase 6 domain models: one person at one target company, walked through a
fixed LinkedIn-first outreach ladder.

The blueprint's red line applies to the whole phase: **LinkedIn is never
automated** — the pipeline drafts text, tracks state, and computes what is due;
every connect/message is sent by the human, who then flips the row's Status on
the sheet (or the pipeline's timers escalate it). ``Person.status`` is the one
state machine::

    queued -> connect_drafted -> connect_sent -> accepted -> message_drafted
        -> message_sent -> replied            (success — conversation is live)
    connect_sent  --no accept in N days-->  email_due   (Phase 6b drafts the email)
    message_sent  --no reply  in N days-->  email_due
    email_due -> email_drafted -> email_sent -> replied  (Phase 6b: the email escalation)
    any ----------------------------------> closed      (human-set, row leaves the sheet)

Phase 6b (email escalation) picks up at ``email_due``: the pipeline drafts a cold
EMAIL (not a LinkedIn note), resolves a recipient, and — for a verified address —
lands it as a Gmail draft for the human to send. ``email_sent`` is the human's
"I sent it" flip; a reply then closes the loop the same way the LinkedIn path does.

Statuses the human may set (sheet dropdown) are ``HUMAN_SETTABLE``; the pipeline
owns the rest. Transitions only move forward (``allowed_human_transition``), with
one exception: a late accept (``email_due -> accepted``) revives the LinkedIn path.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel

STATUS_QUEUED = "queued"
STATUS_CONNECT_DRAFTED = "connect_drafted"
STATUS_CONNECT_SENT = "connect_sent"
STATUS_ACCEPTED = "accepted"
STATUS_MESSAGE_DRAFTED = "message_drafted"
STATUS_MESSAGE_SENT = "message_sent"
STATUS_EMAIL_DUE = "email_due"
STATUS_EMAIL_DRAFTED = "email_drafted"  # Phase 6b: escalation email drafted, awaiting send
STATUS_EMAIL_SENT = "email_sent"  # Phase 6b: the human sent the escalation email
STATUS_REPLIED = "replied"
STATUS_CLOSED = "closed"

# Lifecycle order — the sheet dropdown shows all of them; forward-only validation
# uses the index. The email-escalation states (email_due -> email_drafted ->
# email_sent) sit before replied so a stalled thread can still be marked replied
# (or closed) by the human at any point along the way.
STATUS_ORDER: list[str] = [
    STATUS_QUEUED,
    STATUS_CONNECT_DRAFTED,
    STATUS_CONNECT_SENT,
    STATUS_ACCEPTED,
    STATUS_MESSAGE_DRAFTED,
    STATUS_MESSAGE_SENT,
    STATUS_EMAIL_DUE,
    STATUS_EMAIL_DRAFTED,
    STATUS_EMAIL_SENT,
    STATUS_REPLIED,
    STATUS_CLOSED,
]
_ORDER_INDEX = {s: i for i, s in enumerate(STATUS_ORDER)}

# What the human may set from the sheet. Everything else is pipeline-owned; an
# out-of-band value in the Status cell is reverted on the next sync. ``email_sent``
# is the human's "I sent the escalation email" flip (Phase 6b).
HUMAN_SETTABLE: frozenset[str] = frozenset(
    {
        STATUS_CONNECT_SENT,
        STATUS_ACCEPTED,
        STATUS_MESSAGE_SENT,
        STATUS_EMAIL_SENT,
        STATUS_REPLIED,
        STATUS_CLOSED,
    }
)

# No timers run past these; ``closed`` rows are also removed from the sheet.
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_REPLIED, STATUS_CLOSED})


def allowed_human_transition(current: str, new: str) -> bool:
    """True if the human may move a row ``current -> new`` from the sheet.

    Forward-only (a backward edit is almost always a mis-click and would confuse
    the timers), restricted to ``HUMAN_SETTABLE`` targets, with one deliberate
    exception: ``email_due -> accepted`` — a connect accepted after the escalation
    window rejoins the normal LinkedIn ladder.
    """
    if new not in HUMAN_SETTABLE or current == new:
        return False
    if current == STATUS_EMAIL_DUE and new == STATUS_ACCEPTED:
        return True
    return _ORDER_INDEX.get(new, -1) > _ORDER_INDEX.get(current, len(STATUS_ORDER))


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-")


def make_person_id(campaign: str, company_name: str, index: int) -> str:
    """Stable row identity: ``<campaign>-<company>-<n>``.

    ``index`` is the 1-based position in the company's ``people`` list (a company
    with no people gets one placeholder row at index 1, which the first listed
    person later claims). Positional on purpose — the id must not change when a
    name is filled in later via the sheet — so the targets file documents:
    append new people at the END of a company's list, don't reorder.
    """
    return f"{_slug(campaign)}-{_slug(company_name)}-{index}"


class Person(BaseModel):
    """One person (or a to-be-identified placeholder) at one target company,
    for the ``people`` table. Keyed by ``make_person_id``; company context is
    denormalized on the row so drafting needs no join back to the targets file.
    """

    person_id: str
    campaign: str = "default"
    company_name: str
    company_domain: Optional[str] = None  # feeds the Phase-6b email lookup
    company_website: Optional[str] = None
    company_linkedin: Optional[str] = None  # where the human goes to find people
    company_blurb: str = ""  # generic company facts drafting may use
    # One SPECIFIC thing about the company the human researched — a product, a
    # model, a launch — written as a noun phrase that can complete a sentence
    # ("Jamba 1.6, their hybrid SSM-Transformer model"). The blurb is marketing
    # copy every competitor could also claim; this is what makes a draft land.
    company_hook: str = ""
    tier: int = 2  # 1 = earliest/most reachable; drafting budget is spent tier 1 first

    # Identity — filled from the targets file or by the human on the sheet.
    name: Optional[str] = None
    role: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None  # optional seed; Phase 6b can look it up instead
    # What THIS person has done, as a noun phrase ("building end-to-end
    # fraud-detection pipelines"). Same job as ``company_hook`` for the recipient:
    # without it a draft can only ever talk about the candidate. Roster-only —
    # there is no sheet column, so it never round-trips through ``merge.py``.
    background: str = ""

    status: str = STATUS_QUEUED
    status_changed_at: Optional[str] = None  # ISO; escalation timers measure from here

    # The current artifact awaiting the human (one at a time by design).
    draft_kind: Optional[str] = None  # "connect" | "message" | "email" (Phase 6b)
    draft_subject: Optional[str] = None  # email only (Phase 6b)
    draft_body: str = ""
    used_llm: bool = False

    # Phase 6b: when the escalation email is a verified address it lands as a real
    # Gmail draft (never sent); these hold the created draft so re-runs don't
    # duplicate it and the digest can deep-link to it.
    gmail_draft_id: Optional[str] = None
    gmail_draft_link: Optional[str] = None

    def has_identity(self) -> bool:
        """True once there is someone to actually contact."""
        return bool((self.name or "").strip() or (self.linkedin_url or "").strip())

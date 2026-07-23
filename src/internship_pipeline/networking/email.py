"""Phase 6b: land the escalation email as a real Gmail DRAFT (never send it).

Mirrors ``outreach/drafts.py`` (Phase 5) but the artifact lives on the ``Person``
row, not an ``Outreach`` record. A row that stalled on LinkedIn and had its
escalation email drafted (``email_drafted``) becomes a Gmail draft only when there
is a real recipient address to send to — a seeded ``person.email``. Everything
else stays as the drafted copy in the digest/sheet with a best-guess address for
the human to complete (MVP: no paid contact lookup).

Gates, in order: the sender identity is configured; the CAN-SPAM physical address
is set (never draft a non-compliant email — the body already carries the footer,
baked by the stage exactly like Phase-3 outreach); the recipient isn't suppressed;
and the row isn't already drafted. Idempotent: a row that already has a
``gmail_draft_id`` is skipped on re-runs.
"""

from __future__ import annotations

from typing import Optional

from ..config import Settings
from ..logging_config import get_logger
from ..outreach.footer import is_address_placeholder
from ..outreach.gmail import DraftFn, draft_web_link
from ..outreach.suppress import is_suppressed
from ..storage import Storage
from .models import STATUS_EMAIL_DRAFTED, Person

log = get_logger(__name__)


def eligible_for_email_draft(person: Person) -> bool:
    """True when this row's escalation email should become a real Gmail draft.

    Requires a concrete recipient address (a seeded/known ``email``) — a guessed
    pattern never becomes a ready-to-send draft, matching Phase-5's verified-only
    rule; those stay flagged in the digest for the human to complete.
    """
    return (
        person.status == STATUS_EMAIL_DRAFTED
        and person.draft_kind == "email"
        and not person.gmail_draft_id
        and bool((person.email or "").strip())
    )


def _sender_identity(settings: Settings) -> Optional[str]:
    email = (settings.outreach_from_email or "").strip()
    if not email:
        return None
    name = settings.outreach_from_name.strip()
    return f"{name} <{email}>" if name else email


def create_networking_email_drafts(
    people: list[Person],
    *,
    settings: Settings,
    storage: Storage,
    draft_fn: DraftFn,
) -> int:
    """Create Gmail drafts for every eligible escalation email; returns how many.

    Each success records the draft id + web link on the row (shown in the digest);
    the row stays ``email_drafted`` until the human sends and flips Status to
    ``email_sent``. A failed create is logged and skipped — the drafted copy is
    still on the row for the human to send manually.
    """
    sender = _sender_identity(settings)
    if sender is None:
        log.warning("OUTREACH_FROM_EMAIL unset; skipping networking email drafts")
        return 0
    if is_address_placeholder(settings):
        log.warning(
            "CAN-SPAM physical address is still the placeholder; "
            "skipping networking email drafts (set OUTREACH_PHYSICAL_ADDRESS)"
        )
        return 0

    created = 0
    for person in people:
        if not eligible_for_email_draft(person):
            continue
        if is_suppressed(person.email or "", storage, settings):
            log.info(
                "networking email recipient suppressed; skipping",
                extra={"person_id": person.person_id},
            )
            continue
        try:
            draft_id, message_id = draft_fn(
                sender, person.email, person.draft_subject or "", person.draft_body
            )
        except Exception as exc:  # skip-on-error: the drafted copy is still usable
            log.warning(
                "networking gmail draft creation failed; row stays email_drafted",
                extra={"person_id": person.person_id, "error": repr(exc)},
            )
            continue
        person.gmail_draft_id = draft_id
        person.gmail_draft_link = draft_web_link(message_id)
        storage.save_person(person)
        created += 1
        log.info(
            "networking gmail draft created",
            extra={"person_id": person.person_id, "draft_id": draft_id},
        )
    return created

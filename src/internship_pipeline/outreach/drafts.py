"""Create real Gmail DRAFTS for outreach (Phase 5) — drafting, never sending.

Builds up the outreach channel: instead of the CLI being the only path, each
qualifying drafted email lands in Gmail's Drafts folder where the user edits and
hits send themselves. The human gate is intact — ``drafts.create`` transmits
nothing — and ``approve_and_send`` keeps working for those who prefer it.

Gates, in order: the feature flag is on; the row is an email (LinkedIn is never
touched); it isn't suppressed or already drafted/sent; the contact address is
**verified** (a pattern guess or low-confidence hit never gets a ready-to-send
draft — those stay flagged in the digest exactly as before); and a CAN-SPAM
sender identity is configured. Idempotent: rows that already carry a
``gmail_draft_id`` are skipped on re-runs.
"""

from __future__ import annotations

from typing import Optional

from ..config import Settings
from ..logging_config import get_logger
from ..models import Outreach
from ..storage import Storage
from .gmail import DraftFn, draft_web_link

log = get_logger(__name__)


def eligible_for_gmail_draft(outreach: Outreach) -> bool:
    """True when this outreach row should become a real Gmail draft."""
    return (
        outreach.channel == "email"
        and not outreach.suppressed
        and outreach.status == "pending_review"
        and not outreach.gmail_draft_id
        and bool((outreach.contact_email or "").strip())
        and outreach.contact_verified
    )


def _sender_identity(settings: Settings) -> Optional[str]:
    email = (settings.outreach_from_email or "").strip()
    if not email:
        return None
    name = settings.outreach_from_name.strip()
    return f"{name} <{email}>" if name else email


def create_gmail_drafts(
    drafts: list[Outreach],
    *,
    settings: Settings,
    storage: Storage,
    draft_fn: DraftFn,
) -> int:
    """Create Gmail drafts for every eligible outreach row; returns how many.

    Each success transitions the row ``pending_review -> gmail_draft_created`` and
    records the draft id + web link (shown in the digest). A failed create is
    logged and skipped — the row stays ``pending_review`` for the CLI path.
    """
    sender = _sender_identity(settings)
    if sender is None:
        log.warning("OUTREACH_FROM_EMAIL unset; skipping Gmail draft creation")
        return 0

    created = 0
    for outreach in drafts:
        if not eligible_for_gmail_draft(outreach):
            continue
        try:
            draft_id, message_id = draft_fn(
                sender, outreach.contact_email, outreach.subject or "", outreach.body
            )
        except Exception as exc:  # skip-on-error: the CLI send path still works
            log.warning(
                "gmail draft creation failed; row stays pending_review",
                extra={"outreach_id": outreach.outreach_id, "error": repr(exc)},
            )
            continue
        outreach.gmail_draft_id = draft_id
        outreach.gmail_draft_link = draft_web_link(message_id)
        outreach.status = "gmail_draft_created"
        storage.save_outreach(outreach)
        created += 1
        log.info(
            "gmail draft created",
            extra={"outreach_id": outreach.outreach_id, "draft_id": draft_id},
        )
    return created

"""Manual approve-and-send command (Phase 3) — the human send gate.

    python -m internship_pipeline.outreach.approve_and_send <outreach_id>          # PREVIEW only
    python -m internship_pipeline.outreach.approve_and_send <outreach_id> --yes    # actually send

The daily pipeline NEVER sends. Even this command does not transmit unless you add
``--yes`` — a bare run just prints the message for review. Before any send it enforces,
in order: the channel is email (LinkedIn is draft-only — send those yourself), the
draft isn't already sent, a real recipient exists, the contact is NOT on the
suppression list, the CAN-SPAM footer is present, and the physical address is real
(not the placeholder). Only then is the Gmail send path reached.

Sending is done through an injectable ``SendFn`` so tests exercise the whole gate with
a fake sender — no real email is ever sent from a test.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..config import Settings, get_settings
from ..logging_config import configure_logging, get_logger
from ..models import Outreach
from ..storage import Storage, get_storage
from .footer import has_footer, is_address_placeholder
from .gmail import SendFn, default_send_fn
from .suppress import is_suppressed

log = get_logger(__name__)


@dataclass
class SendOutcome:
    sent: bool
    status: str  # see the status strings raised below
    message: str
    outreach: Optional[Outreach] = None


def _sender_identity(settings: Settings) -> str:
    email = (settings.outreach_from_email or "").strip()
    name = settings.outreach_from_name.strip()
    return f"{name} <{email}>" if name else email


def preview_text(outreach: Outreach, settings: Settings) -> str:
    """Human-readable preview of exactly what would be sent."""
    return (
        f"--- OUTREACH PREVIEW [{outreach.outreach_id}] ---\n"
        f"channel: {outreach.channel}\n"
        f"company: {outreach.company_name} — {outreach.title}\n"
        f"from:    {_sender_identity(settings)}\n"
        f"to:      {outreach.contact_email or '(no recipient — fill one in)'} "
        f"[{outreach.contact_source}, verified={outreach.contact_verified}]\n"
        f"subject: {outreach.subject or ''}\n"
        f"---\n{outreach.body}\n--- END PREVIEW ---"
    )


def send_outreach(
    outreach_id: str,
    *,
    settings: Settings,
    storage: Storage,
    send_fn: Optional[SendFn] = None,
    confirm: bool = False,
) -> SendOutcome:
    """Run the full send gate for one outreach id. Sends only when ``confirm`` is True.

    Returns a ``SendOutcome`` describing what happened. Every refusal path returns
    ``sent=False`` without contacting Gmail.
    """
    outreach = storage.get_outreach(outreach_id)
    if outreach is None:
        return SendOutcome(False, "not_found", f"no outreach with id {outreach_id!r}")

    # --- GUARDRAIL: LinkedIn is a ban-risk red zone; the system never sends it. ---
    if outreach.channel != "email":
        return SendOutcome(
            False, "refused_linkedin",
            f"channel is {outreach.channel!r}: LinkedIn notes are draft-only — send it "
            "yourself, manually, from LinkedIn. This tool only sends email.",
            outreach,
        )

    if outreach.status == "sent":
        return SendOutcome(False, "already_sent", "already sent; refusing to re-send", outreach)

    if not (outreach.contact_email or "").strip():
        return SendOutcome(
            False, "no_recipient",
            "no recipient address (the contact was only a guessed pattern). Add a real "
            "email to this outreach row before sending.",
            outreach,
        )

    # --- SUPPRESSION: enforced before any send (DB list + optional seed file). ---
    if outreach.suppressed or is_suppressed(outreach.contact_email, storage, settings):
        if not outreach.suppressed:  # persist the flag so the tracker reflects reality
            outreach.suppressed = True
            outreach.status = "suppressed"
            storage.save_outreach(outreach)
        return SendOutcome(
            False, "suppressed",
            f"{outreach.contact_email} is on the suppression list — send blocked.",
            outreach,
        )

    # --- CAN-SPAM: honest footer + a real physical address are mandatory. ---
    if not has_footer(outreach.body):
        return SendOutcome(False, "missing_footer", "email body is missing the CAN-SPAM opt-out footer", outreach)
    if is_address_placeholder(settings):
        return SendOutcome(
            False, "placeholder_address",
            "OUTREACH_PHYSICAL_ADDRESS is still the placeholder — CAN-SPAM requires a real "
            "mailing address. Set it before sending (see ACTIONS_FOR_PAUL.md).",
            outreach,
        )
    if not (settings.outreach_from_email or "").strip():
        return SendOutcome(False, "no_sender", "OUTREACH_FROM_EMAIL is unset — set an honest From address", outreach)

    # --- DEFAULT: never auto-send. A bare run is a preview only. ---
    if not confirm:
        return SendOutcome(False, "preview", "preview only — re-run with --yes to send", outreach)

    if send_fn is None:
        send_fn = default_send_fn(settings)
    if send_fn is None:
        return SendOutcome(
            False, "not_configured",
            "Gmail is not configured (set GMAIL_OAUTH_TOKEN_JSON and install the 'gmail' extra)",
            outreach,
        )

    try:
        message_id = send_fn(_sender_identity(settings), outreach.contact_email, outreach.subject or "", outreach.body)
    except Exception as exc:  # never crash the operator's shell; record and report
        outreach.status = "failed"
        storage.save_outreach(outreach)
        log.exception("gmail send failed", extra={"outreach_id": outreach_id})
        return SendOutcome(False, "failed", f"send failed: {exc!r}", outreach)

    outreach.status = "sent"
    outreach.sent_at = datetime.now(timezone.utc).isoformat()
    outreach.provider_message_id = message_id
    storage.save_outreach(outreach)
    log.info("outreach sent", extra={"outreach_id": outreach_id, "message_id": message_id})
    return SendOutcome(True, "sent", f"sent (message id {message_id})", outreach)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or send a drafted outreach email (human-gated).")
    parser.add_argument("outreach_id", help="the outreach id to send (see the tracker)")
    parser.add_argument("--yes", "--send", action="store_true", dest="confirm",
                        help="actually send. Without this flag the command only previews.")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(args.log_level or settings.log_level)
    storage = get_storage(settings)
    try:
        outreach = storage.get_outreach(args.outreach_id)
        if outreach is not None:
            print(preview_text(outreach, settings))
        outcome = send_outreach(args.outreach_id, settings=settings, storage=storage, confirm=args.confirm)
    finally:
        storage.close()

    print(("SENT: " if outcome.sent else "NOT SENT: ") + outcome.message)
    return 0 if (outcome.sent or outcome.status == "preview") else 1


if __name__ == "__main__":
    raise SystemExit(main())

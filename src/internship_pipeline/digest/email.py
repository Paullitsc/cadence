"""Send the morning digest to yourself via Gmail (Phase 4).

This is the ONE outbound action the daily run may perform automatically — and only
because the recipient is *you*. It is gated on ``DIGEST_EMAIL_ENABLED`` plus a
configured recipient and Gmail token; anything missing → it quietly skips (the digest
file is always written regardless). It never sends outreach or anything to a third party.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from ..config import Settings
from ..logging_config import get_logger
from ..outreach.gmail import build_service, send_message

log = get_logger(__name__)

# (sender, to, subject, text, html) -> provider message id
HtmlSendFn = Callable[[str, str, str, str, str], str]


def send_digest_email(
    *,
    html: str,
    text: str,
    settings: Settings,
    subject: Optional[str] = None,
    send_fn: Optional[HtmlSendFn] = None,
) -> bool:
    """Email the digest to yourself. Returns True only if actually sent."""
    if not settings.digest_email_enabled:
        return False
    to = settings.digest_recipient
    sender = (settings.outreach_from_email or to or "").strip()
    if not to or not sender:
        log.warning("digest email skipped: set OUTREACH_FROM_EMAIL / DIGEST_TO_EMAIL")
        return False

    subject = subject or f"Internship digest — {datetime.now(timezone.utc):%Y-%m-%d}"
    if send_fn is None:
        service = build_service(settings, scopes=[settings.gmail_send_scope])
        if service is None:
            log.info("digest email skipped: Gmail not configured")
            return False

        def send_fn(s: str, t: str, subj: str, txt: str, h: str) -> str:  # noqa: E306
            return send_message(service, s, t, subj, txt, h)

    try:
        message_id = send_fn(sender, to, subject, text, html)
    except Exception as exc:  # a failed digest email must not fail the run
        log.warning("digest email send failed", extra={"error": repr(exc)})
        return False
    log.info("digest email sent", extra={"to": to, "message_id": message_id})
    return True

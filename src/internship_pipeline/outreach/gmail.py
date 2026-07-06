"""Gmail send path (Phase 3) — used ONLY by the manual approve-and-send command.

Nothing here runs during the daily pipeline. Sending an email is a human-gated,
outward-facing action, so the actual transmit is reached only from
``approve_and_send`` after an explicit confirmation. The Google client libraries are
lazy-imported and optional; with no token configured ``build_service`` returns None
and the caller refuses to send (it never silently succeeds).

The public transmit unit is a ``SendFn`` — ``(sender, to, subject, body) -> message_id`` —
so the approve-and-send flow depends on a plain callable and tests inject a fake that
records the call instead of contacting Gmail. Real email is never sent in tests.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional

from ..config import Settings
from ..logging_config import get_logger

log = get_logger(__name__)

# (sender, to, subject, body) -> provider message id
SendFn = Callable[[str, str, str, str], str]


class GmailError(RuntimeError):
    """Raised when Gmail is not configured or a send fails."""


def build_raw_message(sender: str, to: str, subject: str, body: str, html: Optional[str] = None) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API.

    ``body`` is the plain-text part; when ``html`` is given it is added as the richer
    alternative (used by the morning digest). Recipients without HTML see ``body``.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def load_user_credentials(settings: Settings, scopes: Optional[list[str]] = None):  # -> Credentials | None
    """Load (and refresh) the authorized-user OAuth credentials, or None if unavailable.

    Shared by Gmail (send/readonly/compose) and, since service accounts have no Drive
    storage quota on personal Google accounts, the Phase 5 Drive upload — both read the
    same token minted via ``gmail_auth``. Degrades to None on any failure so callers
    no-op instead of crashing.
    """
    token_path = settings.gmail_oauth_token_json
    if not token_path:
        return None
    p = Path(token_path).expanduser()
    if not p.exists() or not p.read_text(encoding="utf-8").strip():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        log.warning("google api libraries not installed; install the 'gmail' extra")
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(p), scopes or settings.gmail_scopes)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
    except Exception as exc:  # malformed/expired-unrefreshable token → not configured
        log.warning("could not load Google OAuth credentials", extra={"error": repr(exc)})
        return None
    return creds


def build_service(settings: Settings, scopes: Optional[list[str]] = None):  # -> resource | None
    """Build an authenticated Gmail service, or None if not configured/available.

    Reads the authorized-user token JSON at ``GMAIL_OAUTH_TOKEN_JSON`` (minted once via
    ``gmail_auth`` with both send + readonly scopes). Returns None — so every caller
    degrades gracefully instead of crashing — when the token path is unset, the file is
    missing/empty, the Google libraries aren't installed, or the credentials won't load.
    """
    creds = load_user_credentials(settings, scopes)
    if creds is None:
        log.info("Gmail token file missing/empty or unset; Gmail is not configured")
        return None
    try:
        from googleapiclient.discovery import build
    except ImportError:
        log.warning("google api libraries not installed; install the 'gmail' extra")
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_message(service, sender: str, to: str, subject: str, body: str, html: Optional[str] = None) -> str:
    """Send one message through a Gmail service resource. Returns the provider message id."""
    raw = build_raw_message(sender, to, subject, body, html)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", "")


def make_send_fn(service) -> SendFn:
    """Wrap a Gmail service resource into a simple ``SendFn``."""

    def _send(sender: str, to: str, subject: str, body: str) -> str:
        return send_message(service, sender, to, subject, body)

    return _send


def default_send_fn(settings: Settings) -> Optional[SendFn]:
    """Build the live ``SendFn`` from settings, or None if Gmail isn't configured."""
    service = build_service(settings)
    return None if service is None else make_send_fn(service)


# --- Phase 5: Gmail DRAFTS for outreach (drafting, not sending) -------------------
# (sender, to, subject, body) -> (draft id, draft message id)
DraftFn = Callable[[str, str, str, str], tuple[str, str]]


def create_draft(service, sender: str, to: str, subject: str, body: str) -> tuple[str, str]:
    """Create one Gmail draft; returns (draft id, draft message id).

    Requires the ``gmail.compose`` scope on the token (re-mint via ``gmail_auth``
    with OUTREACH_GMAIL_DRAFTS_ENABLED=true — see ACTIONS_FOR_PAUL.md). This never
    sends anything: the draft sits in Gmail for the human to edit and send.
    """
    raw = build_raw_message(sender, to, subject, body)
    draft = (
        service.users().drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    return draft.get("id", ""), (draft.get("message") or {}).get("id", "")


def draft_web_link(message_id: str) -> str:
    """Best-effort URL that opens the draft in the Gmail web UI.

    # VERIFY: the ``#drafts?compose=<message id>`` fragment is the commonly-working
    # deep link but is not a documented API contract; if it stops resolving, the
    # link still lands on the Drafts folder — nothing breaks.
    """
    base = "https://mail.google.com/mail/u/0/#drafts"
    return f"{base}?compose={message_id}" if message_id else base


def default_draft_fn(settings: Settings) -> Optional[DraftFn]:
    """Build the live ``DraftFn`` from settings, or None if Gmail isn't configured."""
    service = build_service(settings)
    if service is None:
        return None

    def _draft(sender: str, to: str, subject: str, body: str) -> tuple[str, str]:
        return create_draft(service, sender, to, subject, body)

    return _draft

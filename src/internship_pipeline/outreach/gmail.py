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
from typing import Callable, Optional

from ..config import Settings
from ..logging_config import get_logger

log = get_logger(__name__)

# (sender, to, subject, body) -> provider message id
SendFn = Callable[[str, str, str, str], str]


class GmailError(RuntimeError):
    """Raised when Gmail is not configured or a send fails."""


def build_raw_message(sender: str, to: str, subject: str, body: str) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API."""
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def build_service(settings: Settings):  # -> googleapiclient resource | None
    """Build an authenticated Gmail service, or None if not configured/available.

    Reads the authorized-user token JSON at ``GMAIL_OAUTH_TOKEN_JSON`` (minted once
    via ``gmail_auth``). Returns None (so the caller refuses to send) when the token
    path is unset or the Google libraries are not installed.
    """
    token_path = settings.gmail_oauth_token_json
    if not token_path:
        log.info("GMAIL_OAUTH_TOKEN_JSON unset; Gmail send is not configured")
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        log.warning("google api libraries not installed; install the 'gmail' extra to send")
        return None

    creds = Credentials.from_authorized_user_file(token_path, [settings.gmail_send_scope])
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def make_send_fn(service) -> SendFn:
    """Wrap a Gmail service resource into a simple ``SendFn``."""

    def _send(sender: str, to: str, subject: str, body: str) -> str:
        raw = build_raw_message(sender, to, subject, body)
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return sent.get("id", "")

    return _send


def default_send_fn(settings: Settings) -> Optional[SendFn]:
    """Build the live ``SendFn`` from settings, or None if Gmail isn't configured."""
    service = build_service(settings)
    return None if service is None else make_send_fn(service)

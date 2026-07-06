"""Recruiter-reply scan for the morning digest (Phase 4).

A best-effort Gmail search that surfaces recent inbound messages worth a look, so the
digest can point at "you may have replies waiting." It is a **heuristic**, not a
thread-precise match to sent outreach (we don't send from the daily run, so there's no
reliable thread handle) — the digest labels it as "possible replies to review."

Split mirrors the rest of the codebase: ``parse_message`` is pure (fixture-tested); the
Gmail calls are isolated in ``fetch_replies`` and only reached through ``scan_replies``,
which returns ``[]`` (never raises) whenever Gmail isn't configured or the API errors —
so a missing credential quietly yields an empty section instead of breaking the digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Optional

from ..config import Settings
from ..logging_config import get_logger
from .gmail import build_service

log = get_logger(__name__)


@dataclass
class Reply:
    """One inbound message surfaced for review."""

    message_id: str
    thread_id: str = ""
    from_email: str = ""
    from_name: str = ""
    subject: str = ""
    snippet: str = ""
    date: str = ""

    @property
    def who(self) -> str:
        return self.from_name or self.from_email or "(unknown sender)"


def _header(headers: list[dict], name: str) -> str:
    name = name.lower()
    for h in headers or []:
        if isinstance(h, dict) and (h.get("name") or "").lower() == name:
            return h.get("value") or ""
    return ""


def parse_message(msg: dict[str, Any]) -> Reply:
    """Turn a Gmail ``users.messages.get`` (metadata) resource into a ``Reply``."""
    headers = (msg.get("payload") or {}).get("headers") or []
    display_name, email = parseaddr(_header(headers, "From"))
    return Reply(
        message_id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        from_email=email,
        from_name=display_name,
        subject=_header(headers, "Subject"),
        snippet=(msg.get("snippet") or "").strip(),
        date=_header(headers, "Date"),
    )


def build_query(settings: Settings) -> str:
    """Default Gmail search: recent, inbound, not promotions/social — plus any extra terms."""
    q = (
        f"in:inbox newer_than:{max(1, settings.reply_scan_days)}d "
        "-from:me -category:promotions -category:social"
    )
    extra = (settings.reply_scan_query or "").strip()
    return f"{q} {extra}".strip() if extra else q


def fetch_replies(service, query: str, max_results: int) -> list[Reply]:
    """Run the Gmail search and hydrate each hit's metadata into ``Reply`` objects."""
    listing = (
        service.users().messages()
        .list(userId="me", q=query, maxResults=max(1, max_results))
        .execute()
    )
    replies: list[Reply] = []
    for stub in listing.get("messages", []) or []:
        mid = stub.get("id")
        if not mid:
            continue
        msg = (
            service.users().messages()
            .get(userId="me", id=mid, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        replies.append(parse_message(msg))
    return replies


def scan_replies(settings: Settings, *, service: Optional[Any] = None) -> list[Reply]:
    """Best-effort scan. Returns ``[]`` (logged) if Gmail isn't configured or errors."""
    if service is None:
        service = build_service(settings, scopes=[settings.gmail_read_scope])
    if service is None:
        return []
    try:
        return fetch_replies(service, build_query(settings), settings.reply_scan_max)
    except Exception as exc:  # never let the reply scan break the digest
        log.warning("reply scan failed; skipping", extra={"error": repr(exc)})
        return []


def correlate_replies(replies: list[Reply], sent_outreach: list) -> list:
    """SENT outreach rows whose contact has since written back (Phase 5, pure).

    Matches on the sender address — the one reliable handle the heuristic scan has.
    The caller transitions the matched rows ``sent -> replied`` in storage so the
    outreach lifecycle (drafted → gmail_draft_created → sent → replied) is tracked.
    """
    reply_senders = {(r.from_email or "").strip().lower() for r in replies}
    reply_senders.discard("")
    return [
        o
        for o in sent_outreach
        if o.status == "sent" and (o.contact_email or "").strip().lower() in reply_senders
    ]

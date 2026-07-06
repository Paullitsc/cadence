"""Google auth for the tracker: Sheets via the service account, Drive via the human.

Sheets: one JSON secret (``GOOGLE_SERVICE_ACCOUNT_JSON`` — a file path or the inline
JSON itself, matching how GitHub Actions materializes it) builds the Sheets client.
Editing an existing spreadsheet's cells needs no storage quota, so a bare service
account is fine there.

Drive: service accounts on personal (non-Workspace) Google accounts have ZERO Drive
storage quota, so a bare SA can't create/own files at all (only edit ones it doesn't
own) — sharing the folder as Editor doesn't change that. The Drive client is instead
built from the human's own Gmail OAuth token (``GMAIL_OAUTH_TOKEN_JSON``, re-minted
with the ``drive.file`` scope — see ACTIONS_FOR_PAUL.md), so uploaded CVs are owned by
the human's own quota.

Everything degrades gracefully: missing secret/IDs/token, uninstalled Google
libraries, or malformed credentials all return ``None`` so callers no-op instead of
crashing — the pipeline must run end-to-end with zero credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..config import Settings
from ..logging_config import get_logger
from ..outreach.gmail import load_user_credentials

log = get_logger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


@dataclass
class TrackerServices:
    """Authenticated Google API clients for one sync."""

    sheets: Any  # googleapiclient discovery resource ("sheets", "v4")
    drive: Any  # googleapiclient discovery resource ("drive", "v3")


def tracker_configured(settings: Settings) -> bool:
    """True when the tracker has everything it needs to sync (flag + secret + sheet id)."""
    return bool(
        settings.tracker_sheets_enabled
        and settings.google_service_account_json
        and settings.sheets_spreadsheet_id
    )


def load_service_account_info(value: str) -> Optional[dict]:
    """Parse the service-account secret: a path to a JSON key file OR inline JSON."""
    text = (value or "").strip()
    if not text:
        return None
    if not text.startswith("{"):
        p = Path(text).expanduser()
        if not p.exists():
            log.warning("service-account file not found", extra={"path": text})
            return None
        text = p.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("service-account JSON is malformed", extra={"error": repr(exc)})
        return None
    return info if isinstance(info, dict) else None


def build_drive_service(settings: Settings) -> Any:
    """Build the Drive client from the human's OAuth token, or None if unavailable.

    Uses the Gmail OAuth token (needs the ``drive.file`` scope — re-mint via
    ``gmail_auth`` once the tracker is enabled) rather than the service account: see
    the module docstring for why a bare SA can't own Drive files on a personal
    account. Best-effort — a missing/unscoped token disables CV uploads only; Sheets
    sync still runs.
    """
    creds = load_user_credentials(settings, scopes=[settings.drive_file_scope])
    if creds is None:
        log.info(
            "Drive not configured (GMAIL_OAUTH_TOKEN_JSON missing, or minted before "
            "the tracker was enabled); CV uploads will be skipped — re-mint via "
            "gmail_auth with TRACKER_SHEETS_ENABLED=true set (see ACTIONS_FOR_PAUL.md)"
        )
        return None
    try:
        from googleapiclient.discovery import build
    except ImportError:
        log.warning("google api libraries not installed; install the 'tracker' extra")
        return None
    try:
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        log.warning("could not build Drive client", extra={"error": repr(exc)})
        return None


def build_tracker_services(settings: Settings) -> Optional[TrackerServices]:
    """Build the Sheets (required) + Drive (best-effort) clients for one sync.

    Returns None only when Sheets itself isn't configured/available — Drive comes
    back as ``services.drive is None`` on its own failure so Sheets sync still runs
    without durable CV links (callers already treat a failed/absent upload as a
    skip-on-error, per ``tracker/drive.py``).
    """
    if not tracker_configured(settings):
        log.info(
            "tracker not configured; skipping (set TRACKER_SHEETS_ENABLED, "
            "GOOGLE_SERVICE_ACCOUNT_JSON, SHEETS_SPREADSHEET_ID — see ACTIONS_FOR_PAUL.md)"
        )
        return None
    info = load_service_account_info(settings.google_service_account_json or "")
    if info is None:
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        log.warning("google api libraries not installed; install the 'tracker' extra")
        return None
    try:
        creds = Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as exc:  # bad key material → tracker disabled, run continues
        log.warning("could not build Google Sheets client", extra={"error": repr(exc)})
        return None
    return TrackerServices(sheets=sheets, drive=build_drive_service(settings))

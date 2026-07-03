"""One-time Gmail OAuth setup (Phase 3) — mint the authorized-user token.

    python -m internship_pipeline.outreach.gmail_auth

Run this ONCE, locally, after creating an OAuth client in Google Cloud and downloading
its client-secrets JSON. It opens a browser for consent (scope: gmail.send only), then
writes the resulting authorized-user token to ``GMAIL_OAUTH_TOKEN_JSON``. The daily
pipeline never touches this; only ``approve-and-send`` uses the token. See
ACTIONS_FOR_PAUL.md for the full walk-through and the exact secret names.

Requires the optional 'gmail' extra (``uv sync --extra gmail``).
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_settings
from ..logging_config import configure_logging, get_logger

log = get_logger(__name__)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    creds_path = settings.gmail_credentials_json
    token_path = settings.gmail_oauth_token_json
    if not creds_path or not token_path:
        print(
            "Set GMAIL_CREDENTIALS_JSON (path to your OAuth client-secrets file) and "
            "GMAIL_OAUTH_TOKEN_JSON (path to write the token to) first — see ACTIONS_FOR_PAUL.md."
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the 'gmail' extra first:  uv sync --extra gmail")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, [settings.gmail_send_scope])
    creds = flow.run_local_server(port=0)  # opens the browser for consent
    Path(token_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    Path(token_path).expanduser().write_text(creds.to_json(), encoding="utf-8")
    print(f"Wrote Gmail token to {token_path}. approve-and-send can now send from your account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

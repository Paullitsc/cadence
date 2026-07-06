"""Failure alerts for the daily run (Phase 4).

GitHub does NOT notify on scheduled-workflow failures, so the daily workflow's
``if: failure()`` step calls this to raise a real alert.

Per the assignment, exactly one channel is implemented and the other is a clearly
marked stub. **Chosen channel: email (Gmail).** ``send_email_alert`` reuses the Phase-3
Gmail send path to email you the failure; ``send_slack_alert`` is a stub that documents
the one-liner needed to enable it (set ``ALERT_CHANNEL=slack`` + ``SLACK_WEBHOOK_URL``).

Both return a bool (sent / not sent) and never raise — an alert must not itself crash the
workflow step. Usage::

    python -m internship_pipeline.alerts email "Daily run failed — see the Actions logs"
    python -m internship_pipeline.alerts            # uses ALERT_CHANNEL + a default message
"""

from __future__ import annotations

import argparse
import sys

from .config import Settings, get_settings
from .logging_config import configure_logging, get_logger
from .outreach.gmail import SendFn, default_send_fn

log = get_logger(__name__)

_SUBJECT = "[internship-pipeline] ⚠️ daily run alert"


def send_email_alert(text: str, settings: Settings, *, send_fn: SendFn | None = None) -> bool:
    """Email the alert to yourself via Gmail. Returns True only if actually sent.

    Degrades to False (logged) when Gmail isn't configured or no recipient/sender is set —
    so a missing credential can never turn an alert into a second failure.
    """
    to = settings.digest_recipient
    sender = (settings.outreach_from_email or to or "").strip()
    if not to or not sender:
        log.warning("email alert skipped: set OUTREACH_FROM_EMAIL / DIGEST_TO_EMAIL")
        return False
    if send_fn is None:
        send_fn = default_send_fn(settings)
    if send_fn is None:
        log.warning("email alert skipped: Gmail not configured (GMAIL_OAUTH_TOKEN_JSON)")
        return False
    try:
        message_id = send_fn(sender, to, _SUBJECT, text)
    except Exception as exc:  # an alert must never crash the workflow step
        log.warning("email alert send failed", extra={"error": repr(exc)})
        return False
    log.info("email alert sent", extra={"to": to, "message_id": message_id})
    return True


def send_slack_alert(text: str, settings: Settings) -> bool:
    """STUB (unused: email was chosen). Enable by implementing this + ALERT_CHANNEL=slack.

    Intended implementation — a single POST to the incoming webhook, no OAuth::

        import httpx
        if not settings.slack_webhook_url:
            return False
        r = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=10)
        return r.is_success

    Left unimplemented on purpose so the codebase carries exactly one live alert path.
    """
    log.warning("slack alert is a stub (ALERT_CHANNEL=email is active); not sending", extra={"text": text})
    return False


def send_alert(text: str, settings: Settings, *, channel: str | None = None) -> bool:
    """Dispatch an alert on the configured channel (default: ``ALERT_CHANNEL``)."""
    channel = (channel or settings.alert_channel).lower()
    if channel == "email":
        return send_email_alert(text, settings)
    if channel == "slack":
        return send_slack_alert(text, settings)
    log.warning("unknown alert channel; no alert sent", extra={"channel": channel})
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a failure alert (Phase 4).")
    parser.add_argument("channel", nargs="?", default=None, choices=["email", "slack"],
                        help="alert channel (default: ALERT_CHANNEL)")
    parser.add_argument("message", nargs="?", default="Daily internship pipeline run failed.",
                        help="alert text")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    sent = send_alert(args.message, settings, channel=args.channel)
    # Exit 0 regardless — the alert is best-effort and must not fail the workflow further.
    print(("sent alert via " + (args.channel or settings.alert_channel)) if sent else "alert not sent (see logs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

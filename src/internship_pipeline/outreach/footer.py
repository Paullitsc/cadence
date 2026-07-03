"""CAN-SPAM compliance for outbound email (Phase 3).

CAN-SPAM permits cold email, but every message must carry: honest sender identity,
a valid physical mailing address, and a working opt-out (blueprint finding #5). This
module builds that footer and gates sending on it — the send path refuses to transmit
an email whose physical address is still the ``REPLACE_ME`` placeholder, so we never
send a non-compliant message.

The footer applies to EMAIL only. LinkedIn notes are draft-only and never get one.
"""

from __future__ import annotations

from ..config import Settings

# Stable markers so callers/tests can assert the footer (and its required parts) are
# present in a composed email body.
OPT_OUT_MARKER = "To opt out"
ADDRESS_PLACEHOLDER_TOKEN = "REPLACE_ME"


def is_address_placeholder(settings: Settings) -> bool:
    """True while the CAN-SPAM physical address is still the shipped placeholder."""
    addr = (settings.outreach_physical_address or "").strip()
    return (not addr) or ADDRESS_PLACEHOLDER_TOKEN in addr


def _opt_out_text(settings: Settings) -> str:
    if settings.outreach_opt_out.strip():
        return settings.outreach_opt_out.strip()
    return 'reply to this email with "unsubscribe" and you will not be contacted again'


def build_footer(settings: Settings) -> str:
    """Build the CAN-SPAM footer: identity + physical address + opt-out."""
    sender = settings.outreach_from_name.strip() or "Sender"
    from_email = (settings.outreach_from_email or "").strip()
    identity = f"{sender}" + (f" · {from_email}" if from_email else "")
    address = (settings.outreach_physical_address or "").strip()
    lines = [
        "--",
        "You're receiving this because I reached out personally about a role at your company.",
        identity,
        address,
        f"{OPT_OUT_MARKER}, {_opt_out_text(settings)}.",
    ]
    return "\n".join(line for line in lines if line)


def build_email_body(copy_body: str, settings: Settings) -> str:
    """Append the CAN-SPAM footer to the drafted copy (the exact text that will send)."""
    return f"{copy_body.rstrip()}\n\n{build_footer(settings)}"


def has_footer(body: str) -> bool:
    """Heuristic: does this email body already carry a CAN-SPAM opt-out footer?"""
    return OPT_OUT_MARKER in (body or "")

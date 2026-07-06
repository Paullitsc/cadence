"""HTML/email daily digest (jinja2 + Gmail).

Phase 1 rendered "new jobs today" to a local file. Phase 4 makes it the single morning
touchpoint (top matches, outreach + applications awaiting review, possible replies) and,
when enabled + credentialed, emails it to yourself. Sending to yourself is the only
outbound action the daily run performs; outreach/submits are always manual.
"""

from .email import send_digest_email
from .render import render_digest, render_digest_text, write_digest

__all__ = ["render_digest", "render_digest_text", "write_digest", "send_digest_email"]

"""HTML/email daily digest (jinja2 + Gmail).

Phase 1 rendered "new jobs today" to a local file; Phase 4 emailed it. Phase 5 slims
it to the OUTREACH channel: a compact count header + one link to the Google Sheet
tracker (the application workspace), outreach drafts (with Gmail-draft links), and
the possible-replies scan. Sending to yourself is still the only outbound action the
daily run performs; outreach sends/submits are always manual.
"""

from .email import send_digest_email
from .render import render_digest, render_digest_text, write_digest

__all__ = ["render_digest", "render_digest_text", "write_digest", "send_digest_email"]

"""HTML daily digest (jinja2).

Phase 1 renders "new jobs today" and WRITES the digest to a local file (and
``latest.html``); real email sending is deliberately deferred to a later phase
(blueprint: humans gate outbound actions).
"""

from .render import render_digest, write_digest

__all__ = ["render_digest", "write_digest"]

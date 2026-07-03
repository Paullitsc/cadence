"""Phase 3: cold-outreach drafting with a strict human-in-the-loop send gate.

Same pure/side-effecting split as ``sourcing/`` and ``resume/``: contact parsing,
email-pattern guessing, copy drafting + grounding, and CAN-SPAM footer building are
testable pure functions; the only side effects are the provider HTTP calls and the
Gmail send — both dependency-injected or lazy-imported, so the pipeline and the test
suite run offline with zero credentials. Sending is never automatic: it happens only
through the manual ``approve_and_send`` command. LinkedIn is draft-only.
"""

from __future__ import annotations

from .contacts import Contact, LookupBudget, find_contact, guess_email_pattern
from .copy import OutreachContent, draft_outreach_copy
from .footer import build_email_body, build_footer, has_footer, is_address_placeholder

__all__ = [
    "Contact",
    "LookupBudget",
    "find_contact",
    "guess_email_pattern",
    "OutreachContent",
    "draft_outreach_copy",
    "build_email_body",
    "build_footer",
    "has_footer",
    "is_address_placeholder",
]

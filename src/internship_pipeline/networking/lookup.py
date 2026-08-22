"""Phase 6b: find the escalation email's recipient address automatically.

The ladder stalls on LinkedIn, the pipeline drafts a cold email — and then needs
somewhere to send it. Until now that was entirely manual: an address Paul had
seeded on the roster, or nothing (the digest showed a pattern GUESS for him to
complete by hand). This module spends a Hunter/Apollo lookup on exactly those
rows instead.

Three properties make this safe to run unattended:

* **Person-targeted, not company-targeted.** ``find_person_contact`` asks for the
  named human's address and discards a hit whose name disagrees. A cold-apply
  contact ("whoever handles recruiting") would be the wrong recipient here — the
  drafted body already greets the person Paul picked off LinkedIn.
* **Verified-only writeback.** A provider hit is stored on ``person.email`` only
  when it comes back ``verified``; a guess or a low-confidence result is counted
  and logged but never persisted. That is what keeps ``email.eligible_for_email_draft``
  honest — it treats any stored address as send-ready, so an unverified one must
  not reach it. Those rows keep their existing "you find the address" digest line.
* **Naturally rare + hard-capped.** Only a row that already burned a full
  connect/message cycle reaches ``email_due``, and each run is bounded by a
  ``LookupBudget`` on top of that. Free tiers are small (Hunter ~25-50/mo).

Off unless ``NETWORKING_EMAIL_LOOKUP_ENABLED`` and a provider is enabled + keyed;
with no client the whole module is a no-op and the pipeline behaves exactly as it
did before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from ..config import Settings
from ..logging_config import get_logger
from ..outreach.contacts import LookupBudget, find_person_contact
from ..storage import Storage
from .models import STATUS_EMAIL_DRAFTED, STATUS_EMAIL_DUE, Person

log = get_logger(__name__)

# The two rungs that need a recipient: one about to have its email drafted, and
# one already drafted that never found an address (so improving the lookup, or
# turning it on later, reaches rows already sitting on the sheet).
_LOOKUP_STATUSES: frozenset[str] = frozenset({STATUS_EMAIL_DUE, STATUS_EMAIL_DRAFTED})


@dataclass
class LookupResult:
    """What one resolution pass did (surfaced as stage counts)."""

    attempted: int = 0
    found: int = 0  # verified addresses written back to storage
    unverified: int = 0  # a provider answered, but not confidently enough to store


def eligible_for_lookup(person: Person) -> bool:
    """True when this row needs an address the pipeline could go find.

    Requires a real *name* — not just ``has_identity()``, which a LinkedIn URL
    alone satisfies. Every provider path here is name-keyed, so a row identified
    only by URL has nothing to look up and stays manual.
    """
    return (
        person.status in _LOOKUP_STATUSES
        and bool((person.name or "").strip())
        and not (person.email or "").strip()
    )


def resolve_person_emails(
    people: list[Person],
    *,
    settings: Settings,
    storage: Storage,
    client: Optional[httpx.Client],
    budget: LookupBudget,
) -> LookupResult:
    """Look up + persist recipient addresses for every eligible row; report counts.

    Mutates the ``Person`` objects in place (so a caller drafting later in the same
    run sees the new address) and saves each verified hit. Skip-on-error per row:
    ``find_person_contact`` already swallows provider failures into the pattern
    guess, so a bad key costs the budget and nothing else.
    """
    result = LookupResult()
    if client is None:
        return result

    for person in people:
        if not eligible_for_lookup(person):
            continue
        if not budget.can_spend():
            log.info(
                "networking email lookup budget exhausted; remaining rows stay manual",
                extra={"person_id": person.person_id},
            )
            break
        result.attempted += 1
        contact = find_person_contact(
            person_name=person.name or "",
            company_name=person.company_name,
            domain=person.company_domain,
            settings=settings,
            client=client,
            budget=budget,
            allow_paid=True,
        )
        if not (contact.email and contact.verified):
            result.unverified += 1
            log.info(
                "no verified address found; row stays manual",
                extra={
                    "person_id": person.person_id,
                    "source": contact.source,
                    "confidence": contact.confidence,
                },
            )
            continue
        person.email = contact.email
        storage.save_person(person)
        result.found += 1
        log.info(
            "resolved networking recipient address",
            extra={
                "person_id": person.person_id,
                "source": contact.source,
                "confidence": contact.confidence,
            },
        )

    if result.attempted:
        log.info(
            "networking email lookup pass complete",
            extra={
                "attempted": result.attempted,
                "found": result.found,
                "unverified": result.unverified,
                "budget_remaining": budget.remaining,
            },
        )
    return result

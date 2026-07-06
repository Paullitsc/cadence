"""Stage: draft outreach (Phase 3 drafting; Phase 4 dual-trigger gating).

Only roles that ``match_and_slice`` marked **dual_trigger** (high-fit AND favorable)
get outreach — every other prepared role is application-only. This keeps the scarce,
sometimes-paid contact lookups pointed at the roles worth the extra push.

For each dual-trigger application prepared by ``match_and_slice``, resolve an outreach contact,
draft a short, specific cold email + a LinkedIn note from the SAME real top bullets
Phase 2 already retrieved (never invented projects), append the CAN-SPAM footer to
the email, flag recipients on the do-not-contact list, and persist two
``pending_review`` ``Outreach`` rows per job (one ``email``, one ``linkedin``).

Cost + honesty guards, all deterministic and offline-safe:
  * Paid contact lookups (Hunter/Apollo) run ONLY when a provider is enabled + keyed,
    only for high-priority roles (unless configured otherwise), and only within a
    hard per-run ``LookupBudget``. Everything else uses the free, never-certain
    company email-pattern guess.
  * NOTHING is sent here. Sending is exclusively the manual ``approve_and_send``
    command, behind an explicit ``--yes``. LinkedIn is draft-only — never sent.

Reads ``ctx.data["prepared"]`` (+ ``ctx.data["resume"]``) from ``match_and_slice``
and writes the drafted rows to ``ctx.data["outreach"]`` for the digest/log stage.
Runs fully offline with zero credentials (pattern-guess contact + deterministic copy).
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import Outreach, StageContext, StageResult, make_outreach_id
from ..outreach import (
    LookupBudget,
    build_email_body,
    draft_outreach_copy,
    find_contact,
)
from ..outreach.suppress import is_suppressed as suppression_check
from ..resume import load_master_resume
from ..resume.llm import build_default_complete
from ..sourcing.http import build_client
from ..storage import get_storage

NAME = "draft_outreach"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    prepared_all = ctx.data.get("prepared", [])
    # Phase 4 dual-trigger gate: outreach only for high-fit AND favorable roles.
    prepared = [p for p in prepared_all if getattr(p, "dual_trigger", False)]
    if not prepared:
        log.info(
            "no dual-trigger roles to draft outreach for",
            extra={"run_id": ctx.run_id, "prepared_total": len(prepared_all)},
        )
        return StageResult(
            name=NAME,
            counts={"outreach_drafted": 0, "dual_trigger_roles": 0},
            notes=f"prepared={len(prepared_all)}, dual_trigger=0",
        )

    resume = ctx.data.get("resume")
    if resume is None:
        try:
            resume = load_master_resume(s.master_resume_file)
        except FileNotFoundError:
            log.warning("master résumé not found; cannot draft outreach", extra={"run_id": ctx.run_id})
            return StageResult(name=NAME, counts={"outreach_drafted": 0}, notes="no master résumé")

    complete = build_default_complete(s)  # None -> deterministic, fully-grounded template

    # Only stand up a network client (and only spend budget) when a paid provider is
    # actually enabled + keyed. Otherwise every contact is the free pattern guess and
    # the whole stage stays offline.
    paid_enabled = bool(
        (s.enable_hunter and s.hunter_api_key) or (s.enable_apollo and s.apollo_api_key)
    )
    budget = LookupBudget(remaining=max(0, s.outreach_max_lookups_per_run))
    client = build_client(s.http_timeout) if paid_enabled else None

    drafts: list[Outreach] = []
    verified = guessed = suppressed_count = 0
    storage = get_storage(s)
    try:
        for item in prepared:
            job = item.job
            high_priority = bool(item.app.human_review)
            allow_paid = paid_enabled and (
                high_priority or not s.outreach_paid_lookup_high_priority_only
            )

            contact = find_contact(
                company_name=job.company_name,
                url=job.url,
                settings=s,
                client=client,
                budget=budget,
                allow_paid=allow_paid,
            )
            content = draft_outreach_copy(
                job=job,
                contact=contact,
                keywords=item.keywords,
                top_bullets=item.top_bullets,
                resume=resume,
                complete=complete,
                human_review=high_priority,
            )

            # Suppression is a do-not-contact concept for the (email) send path: DB list
            # plus the optional seed file the config promises to merge.
            is_suppressed = bool(contact.email) and suppression_check(contact.email, storage, s)
            if contact.verified:
                verified += 1
            else:
                guessed += 1
            if is_suppressed:
                suppressed_count += 1

            key = job.dedupe_key()
            common = dict(
                dedupe_key=key,
                company_name=job.company_name,
                title=job.title,
                url=job.url,
                contact_name=contact.name,
                contact_email=contact.email,
                contact_title=contact.title,
                contact_source=contact.source,
                contact_confidence=contact.confidence,
                contact_verified=contact.verified,
                contact_note=contact.note,
                suppressed=is_suppressed,
                human_review=content.human_review,
                used_llm=content.used_llm,
            )

            email = Outreach(
                outreach_id=make_outreach_id(key, "email"),
                channel="email",
                subject=content.subject,
                # The stored body is the EXACT text that will send — footer included.
                body=build_email_body(content.email_body, s),
                # A suppressed contact is blocked from the send path up front.
                status="suppressed" if is_suppressed else "pending_review",
                **common,
            )
            # LinkedIn is draft-only (never auto-sent), so it carries no CAN-SPAM footer
            # and always stays pending_review for the human to send by hand.
            linkedin = Outreach(
                outreach_id=make_outreach_id(key, "linkedin"),
                channel="linkedin",
                subject=None,
                body=content.linkedin_note,
                status="pending_review",
                **common,
            )
            storage.save_outreach(email)
            storage.save_outreach(linkedin)
            drafts.extend((email, linkedin))

            log.info(
                "drafted outreach",
                extra={
                    "run_id": ctx.run_id, "company": job.company_name, "title": job.title,
                    "contact_source": contact.source, "verified": contact.verified,
                    "suppressed": is_suppressed, "used_llm": content.used_llm,
                },
            )
    finally:
        storage.close()
        if client is not None:
            client.close()

    ctx.data["outreach"] = drafts
    counts = {
        "outreach_drafted": len(drafts),
        "dual_trigger_roles": len(prepared),
        "email_drafts": len(prepared),
        "linkedin_drafts": len(prepared),
        "verified_contacts": verified,
        "guessed_contacts": guessed,
        "suppressed": suppressed_count,
        "paid_lookups_used": max(0, s.outreach_max_lookups_per_run) - budget.remaining,
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"drafted={len(drafts)}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

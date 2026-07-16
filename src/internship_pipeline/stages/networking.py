"""Stage: networking (Phase 6) — advance the LinkedIn-first outreach ladder.

Company-driven, not job-driven: targets come from ``networking_targets.yaml``
(the committed 8VC seed), not from sourcing. Each daily run:

1. **Seeds/refreshes** one ``Person`` row per person (or placeholder) per target
   company — company facts follow the YAML, identity fields fill only while blank.
2. **Absorbs the human's sheet edits** from the Networking tab: who to contact
   (Person/Role/LinkedIn cells) and the ladder events only a human can observe
   (``connect_sent``/``accepted``/``message_sent``/``replied``/``closed``),
   validated forward-only. Storage first, then the sheet is rewritten to match.
3. **Runs the escalation timers**: a sent connect/message that aged past its
   window becomes ``email_due`` (Phase 6b will draft that email).
4. **Drafts** connect notes (top-up to the daily budget, tier 1 first) and
   post-accept messages (always), deterministic or LLM-grounded — see
   ``networking/copy.py``. NOTHING is sent: LinkedIn is never automated
   (blueprint red line); the human copies each draft out by hand.
5. **Projects** the state onto the sheet's Networking tab (rows keyed by the
   hidden person id; ``closed`` rows deleted, storage-first like tracker
   rejections).

Zero-credential behavior: no targets file → one log line and a no-op; no
tracker → storage still advances and the digest still lists the day's actions;
no master résumé → timers/seeding still run, drafting waits.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..logging_config import get_logger
from ..models import StageContext, StageResult
from ..networking import draft_networking_copy, load_targets, rank_bullets, seed_people
from ..networking.models import (
    STATUS_CLOSED,
    STATUS_CONNECT_DRAFTED,
    STATUS_EMAIL_DUE,
    STATUS_MESSAGE_DRAFTED,
    Person,
)
from ..networking.rows import (
    apply_sheet_edits,
    parse_sheet_people,
    plan_closed_removals,
    plan_people_upsert,
)
from ..networking.sequence import DRAFT_CONNECT, MARK_EMAIL_DUE, plan_due
from ..networking.sheet import NETWORKING_TAB, ensure_networking_tab
from ..resume import all_bullets, load_master_resume
from ..resume.llm import build_default_complete
from ..tracker import build_tracker_services
from ..tracker.sheets import apply_plan, delete_rows, read_rows

NAME = "networking"

log = get_logger(__name__)

# Company facts always follow the targets file (Paul edits tiers/blurbs there);
# identity fields are only ever FILLED from it, never overwritten (the sheet is
# the higher-authority channel for who to contact).
_COMPANY_FIELDS = (
    "company_domain", "company_website", "company_linkedin", "company_blurb", "tier",
)
_IDENTITY_FIELDS = ("name", "role", "linkedin_url", "email")


def _refresh_from_seed(existing: Person, seed: Person) -> bool:
    dirty = False
    for field in _COMPANY_FIELDS:
        value = getattr(seed, field)
        if value not in (None, "") and value != getattr(existing, field):
            setattr(existing, field, value)
            dirty = True
    for field in _IDENTITY_FIELDS:
        if not (getattr(existing, field) or "").strip() and getattr(seed, field):
            setattr(existing, field, getattr(seed, field))
            dirty = True
    return dirty


def _sorted(people: list[Person]) -> list[Person]:
    return sorted(people, key=lambda p: (p.tier, p.company_name.lower(), p.person_id))


def run(ctx: StageContext) -> StageResult:  # noqa: PLR0915 - orchestration is linear
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    s = ctx.settings

    campaign, targets = load_targets(s.networking_targets_file)
    if not targets:
        return StageResult(
            name=NAME,
            counts={"networking_connects_drafted": 0, "networking_messages_drafted": 0},
            notes="no networking targets",
        )

    storage = ctx.get_storage()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # 1) Seed new people; refresh company facts / fill blank identities on old ones.
    by_id = {p.person_id: p for p in storage.list_people()}
    seeded = 0
    for seed in seed_people(campaign, targets):
        existing = by_id.get(seed.person_id)
        if existing is None:
            seed.status_changed_at = now_iso
            storage.save_person(seed)
            by_id[seed.person_id] = seed
            seeded += 1
        elif _refresh_from_seed(existing, seed):
            storage.save_person(existing)
    people = _sorted(list(by_id.values()))

    # 2) Absorb the human's sheet edits (identity + ladder events), storage first.
    services = build_tracker_services(s)  # logs the one "not configured" line itself
    sheet_id = None
    existing_rows: list[list[str]] = []
    human_updates = 0
    spreadsheet_id = s.sheets_spreadsheet_id or ""
    if services is not None:
        sheet_id = ensure_networking_tab(services.sheets, spreadsheet_id)
        existing_rows = read_rows(services.sheets, spreadsheet_id, NETWORKING_TAB)
        changed = apply_sheet_edits(people, parse_sheet_people(existing_rows), now_iso=now_iso)
        for person in changed:
            storage.save_person(person)
        human_updates = len(changed)

    # 3) Timers + 4) drafting.
    due = plan_due(
        people,
        now=now,
        daily_connect_budget=s.networking_daily_connects,
        accept_window_days=s.networking_accept_window_days,
        reply_window_days=s.networking_reply_window_days,
    )
    needs_drafting = any(a.action != MARK_EMAIL_DUE for a in due)
    resume = None
    bullets = []
    complete = None
    if needs_drafting:
        try:
            resume = load_master_resume(s.master_resume_file)
            bullets = all_bullets(resume)
            complete = build_default_complete(s)  # None -> deterministic templates
        except FileNotFoundError:
            log.warning(
                "master résumé not found; networking drafts wait until it exists",
                extra={"run_id": ctx.run_id},
            )

    connects = messages = escalated = 0
    for action in due:
        person = action.person
        if action.action == MARK_EMAIL_DUE:
            person.status = STATUS_EMAIL_DUE
            person.status_changed_at = now_iso
            person.draft_kind = None
            person.draft_subject = None
            person.draft_body = ""
            storage.save_person(person)
            escalated += 1
            continue
        if resume is None:
            continue
        top = rank_bullets(resume, bullets, person)
        note, message = draft_networking_copy(
            person=person, resume=resume, top_bullets=top, complete=complete
        )
        if action.action == DRAFT_CONNECT:
            person.status = STATUS_CONNECT_DRAFTED
            person.draft_kind = "connect"
            person.draft_body = note.body
            person.used_llm = note.used_llm
            connects += 1
        else:
            person.status = STATUS_MESSAGE_DRAFTED
            person.draft_kind = "message"
            person.draft_body = message.body
            person.used_llm = message.used_llm
            messages += 1
        person.status_changed_at = now_iso
        storage.save_person(person)

    # 5) Project onto the sheet: drop closed rows, then upsert the rest.
    rows_appended = cells_updated = rows_removed = 0
    if services is not None and sheet_id is not None:
        closed_ids = {p.person_id for p in people if p.status == STATUS_CLOSED}
        removals = plan_closed_removals(existing_rows, closed_ids)
        if removals:
            delete_rows(services.sheets, spreadsheet_id, sheet_id, removals)
            rows_removed = len(removals)
            # Deletions shifted every row below them — re-snapshot before planning.
            existing_rows = read_rows(services.sheets, spreadsheet_id, NETWORKING_TAB)
        plan = plan_people_upsert(
            existing_rows,
            people,
            accept_window_days=s.networking_accept_window_days,
            reply_window_days=s.networking_reply_window_days,
        )
        apply_plan(services.sheets, spreadsheet_id, NETWORKING_TAB, plan)
        rows_appended = len(plan.appends)
        cells_updated = len(plan.updates)

    counts = {
        "networking_people_seeded": seeded,
        "networking_connects_drafted": connects,
        "networking_messages_drafted": messages,
        "networking_escalated": escalated,
        "networking_human_updates": human_updates,
        "networking_rows_appended": rows_appended,
        "networking_cells_updated": cells_updated,
        "networking_rows_removed": rows_removed,
    }
    log.info("stage done", extra={"run_id": ctx.run_id, "stage": NAME, **counts})
    return StageResult(name=NAME, counts=counts, notes=f"campaign={campaign}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

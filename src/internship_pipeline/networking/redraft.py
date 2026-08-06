"""Maintenance: regenerate the copy already sitting on drafted rows.

The ladder drafts each artifact exactly once. ``plan_due`` deliberately skips a
person at ``connect_drafted`` / ``message_drafted`` / ``email_drafted`` — that
row is waiting on the human, not on the pipeline — so when the drafting prompt
improves, or the roster gains a ``hook``/``background`` for that company, the
rows already in flight keep yesterday's copy forever.

This re-runs ``copy.py`` over exactly those rows: same person, same status, same
ladder position, same ``status_changed_at`` (the escalation clocks must not
restart). Only ``draft_body`` / ``draft_subject`` / ``used_llm`` change. Storage
is the only thing written here; run the networking stage afterwards to project
the new text onto the sheet (the Draft column is always-refresh).

    uv run python -m internship_pipeline.networking.redraft [--dry-run] [--kind connect]

An ``email_drafted`` row that already became a real Gmail draft is left alone:
its text is out in a draft the human may already be editing, and rewriting the
stored body would only desynchronize the two.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..logging_config import get_logger
from ..outreach.footer import build_email_body
from ..resume import all_bullets, load_master_resume
from ..resume.llm import build_default_complete
from ..storage import Storage, get_storage
from .copy import draft_networking_copy, draft_networking_email, rank_bullets
from .models import (
    STATUS_CONNECT_DRAFTED,
    STATUS_EMAIL_DRAFTED,
    STATUS_MESSAGE_DRAFTED,
    Person,
)

log = get_logger(__name__)

# status -> the draft_kind that status owns.
REDRAFTABLE: dict[str, str] = {
    STATUS_CONNECT_DRAFTED: "connect",
    STATUS_MESSAGE_DRAFTED: "message",
    STATUS_EMAIL_DRAFTED: "email",
}


@dataclass
class Redraft:
    """One row's before/after, for the caller to print or diff."""

    person: Person
    kind: str
    before: str
    after: str
    used_llm: bool

    @property
    def changed(self) -> bool:
        return self.after.strip() != self.before.strip()


def redraftable(people: list[Person], *, kinds: set[str] | None = None) -> list[Person]:
    """The rows this module may rewrite, tier 1 first (same order as the ladder)."""
    out = [
        p
        for p in people
        if REDRAFTABLE.get(p.status) is not None
        and (kinds is None or REDRAFTABLE[p.status] in kinds)
        and not (p.status == STATUS_EMAIL_DRAFTED and p.gmail_draft_id)
    ]
    return sorted(out, key=lambda p: (p.tier, p.company_name.lower(), p.person_id))


def redraft_people(
    people: list[Person],
    *,
    settings: Settings,
    storage: Storage | None = None,
    kinds: set[str] | None = None,
) -> list[Redraft]:
    """Regenerate every eligible draft; persist when ``storage`` is given.

    ``storage=None`` is the dry run: the new copy is computed and returned but
    nothing is written, so the LLM spend still happens exactly once either way.
    """
    targets = redraftable(people, kinds=kinds)
    if not targets:
        return []

    resume = load_master_resume(settings.master_resume_file)
    bullets = all_bullets(resume)
    complete = build_default_complete(settings)  # None -> deterministic templates
    if complete is None:
        log.warning("no LLM configured; re-drafting with the deterministic templates")

    results: list[Redraft] = []
    for person in targets:
        kind = REDRAFTABLE[person.status]
        before = person.draft_body
        top = rank_bullets(resume, bullets, person)
        if kind == "email":
            email = draft_networking_email(
                person=person, resume=resume, top_bullets=top, complete=complete
            )
            person.draft_subject = email.subject
            # Same as the stage: the footer is baked in so draft_body is the exact,
            # send-ready text.
            person.draft_body = build_email_body(email.body, settings)
            person.used_llm = email.used_llm
        else:
            note, message = draft_networking_copy(
                person=person, resume=resume, top_bullets=top, complete=complete
            )
            content = note if kind == "connect" else message
            person.draft_body = content.body
            person.used_llm = content.used_llm
        person.draft_kind = kind
        results.append(
            Redraft(person, kind, before, person.draft_body, person.used_llm)
        )
        if storage is not None:
            storage.save_person(person)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the new copy without saving it"
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=sorted(set(REDRAFTABLE.values())),
        help="only re-draft this kind (repeatable; default: all)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    storage = get_storage(settings)
    results = redraft_people(
        storage.list_people(),
        settings=settings,
        storage=None if args.dry_run else storage,
        kinds=set(args.kind) if args.kind else None,
    )
    if not results:
        print("nothing to re-draft")
        return 0

    for r in results:
        who = r.person.name or "(unnamed)"
        flag = "llm" if r.used_llm else "template"
        mark = "" if r.changed else "  [unchanged]"
        print(f"\n=== {r.person.company_name} — {who} ({r.kind}, {flag}){mark}")
        print(r.after)
    changed = sum(1 for r in results if r.changed)
    verb = "would rewrite" if args.dry_run else "rewrote"
    print(f"\n{verb} {changed}/{len(results)} draft(s)")
    if not args.dry_run:
        print("run `make roster-sync` to push the new text onto the Networking tab")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

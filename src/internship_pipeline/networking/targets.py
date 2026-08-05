"""Load ``networking_targets.yaml`` into typed targets, seed ``Person`` rows, and
write the file back.

Schema is documented in the header of ``networking_targets.yaml`` itself (the
committed 8VC seed). Mirrors ``sourcing/companies.py``: malformed rows are
skipped with a log line rather than crashing the run, and a missing file means
the whole phase quietly no-ops — the pipeline must run with zero setup.

The file is also a WRITE target: when the human names a person on the sheet's
Networking tab, ``write_targets`` folds that back into the roster so the git
copy stays the durable record (see ``networking/merge.py`` for who wins). The
round-trip deliberately preserves the schema-doc comment block at the top of the
file — everything above the first ``campaign:`` line is copied through verbatim,
since PyYAML cannot carry comments.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from ..logging_config import get_logger
from .models import Person, make_person_id

log = get_logger(__name__)

# PyYAML re-wraps long scalars, so the dump width decides whether a writeback
# shows a real diff or reflows every blurb in the file. 96 reproduces the
# committed 8VC seed's wrapping exactly (verified against it) — keep it in sync
# with the file if it is ever regenerated at another width.
_DUMP_WIDTH = 96

_CAMPAIGN_LINE = re.compile(r"^campaign:", flags=re.MULTILINE)


class TargetPerson(BaseModel):
    """One person listed under a company in the targets file."""

    # Empty is allowed: a row the human identified on the sheet by LinkedIn URL
    # alone still has to round-trip into the file, and dropping the entry would
    # shift every later person's positional id (see ``make_person_id``).
    name: str = ""
    role: Optional[str] = None
    linkedin: Optional[str] = None
    email: Optional[str] = None
    # Hand-researched noun phrase describing what they've worked on — see
    # ``Person.background``. Optional; drafts degrade to candidate-only copy.
    background: str = ""

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class NetworkingTarget(BaseModel):
    """One target company (plus whoever is listed to contact there)."""

    name: str
    tier: int = 2
    stage: Optional[str] = None  # informational only
    website: Optional[str] = None
    domain: Optional[str] = None
    company_linkedin: Optional[str] = None
    blurb: str = ""
    # Hand-researched noun phrase naming something specific — see
    # ``Person.company_hook``. Optional; without it drafts stay generic.
    hook: str = ""
    people: list[TargetPerson] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def load_targets(path: str | Path) -> tuple[str, list[NetworkingTarget]]:
    """Parse the targets file into ``(campaign, targets)``.

    Returns ``("", [])`` when the file is missing (phase not in use) and skips
    malformed company rows individually so one bad edit can't take out the run.
    """
    p = Path(path)
    if not p.exists():
        log.info(
            "networking targets file not found; networking stage will no-op",
            extra={"path": str(path)},
        )
        return "", []

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    campaign = str(raw.get("campaign") or "default").strip() or "default"
    targets: list[NetworkingTarget] = []
    for row in raw.get("companies") or []:
        try:
            target = NetworkingTarget.model_validate(row)
        except Exception as exc:
            log.warning(
                "skipping malformed networking target",
                extra={"row": repr(row)[:200], "error": repr(exc)},
            )
            continue
        if not target.name:
            continue
        targets.append(target)
    log.info(
        "loaded networking targets",
        extra={"path": str(p), "campaign": campaign, "companies": len(targets)},
    )
    return campaign, targets


def _person_to_dict(tp: TargetPerson) -> dict:
    row: dict = {"name": tp.name}
    for key, value in (
        ("role", tp.role),
        ("linkedin", tp.linkedin),
        ("email", tp.email),
        ("background", tp.background),
    ):
        if value:
            row[key] = value
    return row


def _target_to_dict(target: NetworkingTarget) -> dict:
    """One company as the file spells it — declaration key order, no empty keys."""
    row: dict = {"name": target.name, "tier": target.tier}
    for key, value in (
        ("stage", target.stage),
        ("website", target.website),
        ("domain", target.domain),
        ("company_linkedin", target.company_linkedin),
        ("blurb", target.blurb),
        ("hook", target.hook),
    ):
        if value:
            row[key] = value
    if target.people:
        row["people"] = [_person_to_dict(p) for p in target.people]
    return row


def dump_targets(campaign: str, targets: list[NetworkingTarget], *, header: str = "") -> str:
    """Serialize back to the file's own YAML dialect, ``header`` copied through."""
    body = yaml.safe_dump(
        {"campaign": campaign, "companies": [_target_to_dict(t) for t in targets]},
        sort_keys=False,
        allow_unicode=True,
        width=_DUMP_WIDTH,
        default_flow_style=False,
    )
    return f"{header}{body}"


def write_targets(path: str | Path, campaign: str, targets: list[NetworkingTarget]) -> bool:
    """Rewrite the targets file from ``targets``; return True if it changed on disk.

    The schema-doc comment block above ``campaign:`` is preserved. A byte-identical
    result is not written at all, so an unchanged roster never dirties the working
    tree (which is what keeps the CI writeback commit quiet on ordinary days).
    """
    p = Path(path)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    match = _CAMPAIGN_LINE.search(existing)
    header = existing[: match.start()] if match else ""
    updated = dump_targets(campaign, targets, header=header)
    if updated == existing:
        return False
    p.write_text(updated, encoding="utf-8")
    log.info("wrote networking targets file", extra={"path": str(p), "companies": len(targets)})
    return True


def seed_people(campaign: str, targets: list[NetworkingTarget]) -> list[Person]:
    """The deterministic ``Person`` rows this targets file implies.

    Each listed person becomes one row (positional ids — see ``make_person_id``);
    a company with nobody listed gets one identity-less placeholder row that the
    human fills in on the sheet (or by adding a ``people`` entry, which then
    claims the same id). Pure — no storage, no timestamps.
    """
    people: list[Person] = []
    for target in targets:
        company_common = dict(
            campaign=campaign,
            company_name=target.name,
            company_domain=target.domain,
            company_website=target.website,
            company_linkedin=target.company_linkedin,
            company_blurb=target.blurb,
            company_hook=target.hook,
            tier=target.tier,
        )
        if not target.people:
            people.append(
                Person(person_id=make_person_id(campaign, target.name, 1), **company_common)
            )
            continue
        for index, tp in enumerate(target.people, start=1):
            people.append(
                Person(
                    person_id=make_person_id(campaign, target.name, index),
                    name=tp.name,
                    role=tp.role,
                    linkedin_url=tp.linkedin,
                    email=tp.email,
                    background=tp.background,
                    **company_common,
                )
            )
    return people

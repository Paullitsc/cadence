"""Load ``networking_targets.yaml`` into typed targets and seed ``Person`` rows.

Schema is documented in the header of ``networking_targets.yaml`` itself (the
committed 8VC seed). Mirrors ``sourcing/companies.py``: malformed rows are
skipped with a log line rather than crashing the run, and a missing file means
the whole phase quietly no-ops — the pipeline must run with zero setup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from ..logging_config import get_logger
from .models import Person, make_person_id

log = get_logger(__name__)


class TargetPerson(BaseModel):
    """One person listed under a company in the targets file."""

    name: str
    role: Optional[str] = None
    linkedin: Optional[str] = None
    email: Optional[str] = None

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
                    **company_common,
                )
            )
    return people

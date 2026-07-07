"""Greenhouse / Lever / Ashby fetchers, normalized into ``Job``.

Endpoints are EXACTLY the blueprint's (section 2a) — no others invented. The
response field names below were confirmed against the live public feeds on
2026-06-29 (Greenhouse: stripe; Lever: spotify; Ashby: ramp), so they are not
guesses. All three feeds only return live/listed roles, so ``active`` is True
except where a feed exposes an explicit flag (Ashby ``isListed``).

Job descriptions (confirmed live 2026-07-07, same three boards): Greenhouse's
``content`` is HTML-ENTITY-ESCAPED HTML (``&lt;p&gt;``) — decoded and stripped via
``html_to_text``. Lever has no single description field; ``descriptionPlain`` (the
intro) plus each ``lists[]`` section's ``text`` heading + HTML ``content`` (e.g.
"What You'll Do", "Who You Are") together carry the actual requirements — the
``lists[].content`` HTML is stripped the same way. Ashby exposes ``descriptionPlain``
directly, no stripping needed. Capturing this (previously discarded) text is what lets
Phase 2 matching/tailoring score against the real JD instead of just title+company.

Each ``parse_*`` is pure (takes already-parsed JSON) so it is unit-testable from a
fixture with no network; each ``fetch_*`` does the HTTP call.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..logging_config import get_logger
from ..models import Job, JobSource
from .companies import CompanyTarget
from .html_text import html_to_text
from .http import get_json

log = get_logger(__name__)

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


# --- Greenhouse -------------------------------------------------------------
# GET .../jobs?content=true -> {"jobs": [...], "meta": {...}}
# job: absolute_url, location:{name}, title, company_name, first_published,
#      updated_at, content (HTML when content=true), id


def parse_greenhouse(payload: dict[str, Any], *, slug: str, company_name: str) -> list[Job]:
    jobs: list[Job] = []
    for j in payload.get("jobs") or []:
        url = j.get("absolute_url")
        title = j.get("title")
        if not url or not title:
            continue  # defensive: skip incomplete rows
        loc = j.get("location") or {}
        name = loc.get("name") if isinstance(loc, dict) else None
        jobs.append(
            Job(
                company_name=j.get("company_name") or company_name,
                title=title,
                url=url,
                locations=[name] if name else [],
                date_posted=j.get("first_published") or j.get("updated_at"),
                active=True,
                source=f"greenhouse:{slug}",
                source_feed=JobSource.GREENHOUSE,
                description=html_to_text(j.get("content")) or None,
            )
        )
    return jobs


def fetch_greenhouse(
    client: httpx.Client, target: CompanyTarget, *, max_retries: int = 3
) -> list[Job]:
    data = get_json(
        client,
        GREENHOUSE_URL.format(slug=target.slug),
        params={"content": "true"},
        max_retries=max_retries,
    )
    return parse_greenhouse(data, slug=target.slug, company_name=target.name)


# --- Lever ------------------------------------------------------------------
# GET .../postings/{slug}?mode=json -> [ {...}, ... ]
# posting: text (title), hostedUrl, applyUrl, createdAt (epoch ms),
#          categories:{location, allLocations, team, commitment},
#          descriptionPlain (intro), lists:[{text (heading), content (HTML)}]


def _lever_description(p: dict[str, Any]) -> Optional[str]:
    """Join Lever's intro + requirement sections into one description text.

    Lever has no single description field: ``descriptionPlain`` is just the
    intro paragraph; the actual requirements live in ``lists`` — one entry per
    section (e.g. "What You'll Do", "Who You Are"), each with a plain-text
    ``text`` heading and an HTML ``content`` body.
    """
    parts: list[str] = []
    plain = p.get("descriptionPlain")
    if plain:
        parts.append(str(plain))
    for item in p.get("lists") or []:
        if not isinstance(item, dict):
            continue
        heading = item.get("text")
        if heading:
            parts.append(str(heading))
        body = html_to_text(item.get("content"))
        if body:
            parts.append(body)
    return "\n".join(parts) if parts else None


def parse_lever(payload: list[dict[str, Any]], *, slug: str, company_name: str) -> list[Job]:
    jobs: list[Job] = []
    for p in payload or []:
        title = p.get("text")
        url = p.get("hostedUrl") or p.get("applyUrl")
        if not title or not url:
            continue
        cats = p.get("categories") or {}
        locations = cats.get("allLocations") or (
            [cats["location"]] if cats.get("location") else []
        )
        jobs.append(
            Job(
                company_name=company_name,  # Lever feed carries no company name
                title=title,
                url=url,
                locations=[loc for loc in locations if loc],
                date_posted=p.get("createdAt"),  # epoch ms; kept raw, parsed later
                active=True,
                source=f"lever:{slug}",
                source_feed=JobSource.LEVER,
                description=_lever_description(p),
            )
        )
    return jobs


def fetch_lever(
    client: httpx.Client, target: CompanyTarget, *, max_retries: int = 3
) -> list[Job]:
    data = get_json(
        client,
        LEVER_URL.format(slug=target.slug),
        params={"mode": "json"},
        max_retries=max_retries,
    )
    return parse_lever(data, slug=target.slug, company_name=target.name)


# --- Ashby ------------------------------------------------------------------
# GET .../job-board/{slug}?includeCompensation=true -> {"jobs":[...]}
# job: title, location (str), secondaryLocations:[{location, address}],
#      jobUrl, applyUrl, publishedAt (ISO), isListed (bool), employmentType,
#      descriptionPlain (plain text, ready to use — no HTML stripping needed)


def parse_ashby(payload: dict[str, Any], *, slug: str, company_name: str) -> list[Job]:
    jobs: list[Job] = []
    for j in payload.get("jobs") or []:
        if not j.get("isListed", True):
            continue  # only listed/active roles
        title = j.get("title")
        url = j.get("jobUrl") or j.get("applyUrl")
        if not title or not url:
            continue
        locations: list[str] = []
        if j.get("location"):
            locations.append(j["location"])
        for sec in j.get("secondaryLocations") or []:
            # secondaryLocations elements are {"location": str, "address": {...}}
            if isinstance(sec, dict) and sec.get("location"):
                locations.append(sec["location"])
            elif isinstance(sec, str):
                locations.append(sec)
        jobs.append(
            Job(
                company_name=company_name,  # Ashby feed carries no company name
                title=title,
                url=url,
                locations=locations,
                date_posted=j.get("publishedAt"),
                active=bool(j.get("isListed", True)),
                source=f"ashby:{slug}",
                source_feed=JobSource.ASHBY,
                description=j.get("descriptionPlain") or None,
            )
        )
    return jobs


def fetch_ashby(
    client: httpx.Client, target: CompanyTarget, *, max_retries: int = 3
) -> list[Job]:
    data = get_json(
        client,
        ASHBY_URL.format(slug=target.slug),
        params={"includeCompensation": "true"},
        max_retries=max_retries,
    )
    return parse_ashby(data, slug=target.slug, company_name=target.name)


_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_company(
    client: httpx.Client, target: CompanyTarget, *, max_retries: int = 3
) -> list[Job]:
    """Dispatch to the right ATS fetcher for ``target.ats``."""
    return _FETCHERS[target.ats](client, target, max_retries=max_retries)

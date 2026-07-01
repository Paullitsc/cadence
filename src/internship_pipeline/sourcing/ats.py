"""Greenhouse / Lever / Ashby fetchers, normalized into ``Job``.

Endpoints are EXACTLY the blueprint's (section 2a) — no others invented. The
response field names below were confirmed against the live public feeds on
2026-06-29 (Greenhouse: stripe; Lever: spotify; Ashby: ramp), so they are not
guesses. All three feeds only return live/listed roles, so ``active`` is True
except where a feed exposes an explicit flag (Ashby ``isListed``).

Each ``parse_*`` is pure (takes already-parsed JSON) so it is unit-testable from a
fixture with no network; each ``fetch_*`` does the HTTP call.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..logging_config import get_logger
from ..models import Job, JobSource
from .companies import CompanyTarget
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
#          categories:{location, allLocations, team, commitment}


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
#      jobUrl, applyUrl, publishedAt (ISO), isListed (bool), employmentType


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

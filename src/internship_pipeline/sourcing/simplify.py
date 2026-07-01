"""SimplifyJobs ``listings.json`` puller.

Raw path confirmed against the live repo on 2026-06-29:
``raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json``
(branch ``dev``; configurable via ``SIMPLIFY_LISTINGS_URL``). Each row carries the
blueprint's fields plus extras: company_name, title, locations, url, date_posted,
active, source (+ id, terms, degrees, sponsorship, is_visible, ...).

Diffing against what's stored is handled by the jobs-table dedupe (stable hash);
here we just normalize and keep active roles so only NEW active ones surface.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..logging_config import get_logger
from ..models import Job, JobSource
from .http import get_json

log = get_logger(__name__)


def parse_simplify(rows: list[dict[str, Any]], *, active_only: bool = True) -> list[Job]:
    jobs: list[Job] = []
    for r in rows or []:
        if active_only and not r.get("active", False):
            continue
        url = r.get("url")
        title = r.get("title")
        company = r.get("company_name")
        if not url or not title or not company:
            continue
        jobs.append(
            Job(
                company_name=company,
                title=title,
                url=url,
                locations=r.get("locations") or [],
                date_posted=r.get("date_posted"),  # epoch int; kept raw
                active=bool(r.get("active", True)),
                source=r.get("source") or "simplify",
                source_feed=JobSource.SIMPLIFY,
            )
        )
    return jobs


def fetch_simplify(
    client: httpx.Client, url: str, *, max_retries: int = 3, active_only: bool = True
) -> list[Job]:
    rows = get_json(client, url, max_retries=max_retries)
    return parse_simplify(rows, active_only=active_only)

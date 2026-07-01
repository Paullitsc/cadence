"""JSearch (RapidAPI) fetcher — OPTIONAL tertiary source.

Gated behind ``ENABLE_JSEARCH`` and a ``RAPIDAPI_KEY``; the ``source`` stage skips
it cleanly when either is absent. The free BASIC plan is hard-capped at 200
requests/month, so keep ``jsearch_pages`` at 1 (one request per run ≈ 30/month).

Confirmed live against RapidAPI on 2026-07-01: the search endpoint is
``GET /search-v2`` (the plain ``/search`` path used by older docs/blueprints
404s — JSearch has since versioned it), and the response nests jobs one level
deeper than before: ``{"data": {"jobs": [...], "cursor": ...}}`` rather than
``{"data": [...]}``. Per-job field names (``job_title``, ``job_apply_link``,
etc.) are unchanged. Parsing stays defensive regardless (a missing/renamed
field degrades to "skip row" rather than crashing).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..logging_config import get_logger
from ..models import Job, JobSource
from .http import get_json

log = get_logger(__name__)


def parse_jsearch(payload: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    data = payload.get("data") or {}
    rows = data.get("jobs") if isinstance(data, dict) else data
    for d in rows or []:
        title = d.get("job_title")
        url = d.get("job_apply_link") or d.get("job_google_link")
        company = d.get("employer_name")
        if not title or not url or not company:
            continue
        parts = [d.get("job_city"), d.get("job_state"), d.get("job_country")]
        location = ", ".join(p for p in parts if p)
        jobs.append(
            Job(
                company_name=company,
                title=title,
                url=url,
                locations=[location] if location else [],
                date_posted=d.get("job_posted_at_datetime_utc"),
                active=True,
                source="jsearch",
                source_feed=JobSource.JSEARCH,
            )
        )
    return jobs


def fetch_jsearch(
    client: httpx.Client,
    *,
    host: str,
    key: str,
    query: str,
    num_pages: int = 1,
    max_retries: int = 3,
) -> list[Job]:
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    data = get_json(
        client,
        f"https://{host}/search-v2",
        params={"query": query, "page": "1", "num_pages": str(num_pages)},
        headers=headers,
        max_retries=max_retries,
    )
    return parse_jsearch(data)

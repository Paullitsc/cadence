"""JSearch (RapidAPI) fetcher — OPTIONAL tertiary source.

Gated behind ``ENABLE_JSEARCH`` and a ``RAPIDAPI_KEY``; the ``source`` stage skips
it cleanly when either is absent. The free BASIC plan is hard-capped at 200
requests/month, so keep ``jsearch_pages`` at 1 (one request per run ≈ 30/month).

NOTE: unlike the ATS feeds, the JSearch response shape could NOT be probed (it
needs a key), so the field names below are marked ``# VERIFY`` — confirm against a
real response and tell me if any differ. Parsing is defensive, so a wrong field
name degrades to "skip row" rather than crashing.
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
    for d in payload.get("data") or []:
        # VERIFY: JSearch `data[]` field names (employer_name / job_title /
        # job_apply_link / job_city / job_state / job_country /
        # job_posted_at_datetime_utc). Could not probe without a RapidAPI key.
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
        f"https://{host}/search",
        params={"query": query, "page": "1", "num_pages": str(num_pages)},
        headers=headers,
        max_retries=max_retries,
    )
    return parse_jsearch(data)

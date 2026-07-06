"""Real application-form questions from the ATS (Phase 5) — Greenhouse only.

Endpoint + response shape confirmed against a live board (stripe, 2026-07-06):

    GET boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true
    -> { ..., "questions": [ { "label": str, "required": bool, "description": str|null,
         "fields": [ { "name": str, "type": str, "values": [{"label","value"}] } ] } ] }

Observed field types: ``input_text``, ``textarea``, ``input_file``,
``multi_value_single_select``, ``multi_value_multi_select``. Only free-TEXT custom
questions are surfaced for drafting: identity/file boilerplate (name, email, résumé
upload) isn't a question, and select questions (work authorization, degree, ...)
are the human's to answer — drafting a "Yes" there would be fabrication.

Lever and Ashby's public posting APIs were checked the same day and expose **no
application-form fields at all**, so those ATSes (and everything else) fall back to
the standard question set in ``resume/answers.py`` — no HTML scraping.

Split mirrors the rest of ``sourcing/``: ``parse_*`` pure (fixture-tested),
``fetch_*`` does the HTTP.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from ..logging_config import get_logger
from ..models import Job
from .http import get_json

log = get_logger(__name__)

GREENHOUSE_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"

# Boilerplate identity/file fields — present on every board, not draftable questions.
_STANDARD_FIELD_NAMES: frozenset[str] = frozenset(
    """
    first_name last_name email phone resume resume_text cover_letter cover_letter_text
    location
    """.split()
)
_TEXT_TYPES: frozenset[str] = frozenset({"input_text", "textarea"})

# Greenhouse job URLs carry the posting id as ".../jobs/<digits>" on the hosted
# board, or as a "gh_jid" query param on company-domain boards.
_JOBS_PATH_RE = re.compile(r"/jobs/(\d+)(?:/|$)")
_GH_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")


def parse_greenhouse_job_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort (board_token, job_id) from a job URL. Either may be None."""
    parts = urlsplit(url or "")
    host = parts.netloc.lower()
    slug: Optional[str] = None
    job_id: Optional[str] = None

    m = _JOBS_PATH_RE.search(parts.path)
    if m:
        job_id = m.group(1)
    else:
        qs = parse_qs(parts.query)
        gh_jid = (qs.get("gh_jid") or [None])[0]
        if gh_jid and gh_jid.isdigit():
            job_id = gh_jid

    if any(host == h or host.endswith("." + h) for h in _GH_HOSTS):
        segments = [s for s in parts.path.split("/") if s]
        if segments:
            slug = segments[0]
    return slug, job_id


def greenhouse_ref(job: Job) -> Optional[tuple[str, str]]:
    """(board_token, job_id) for a job we can fetch real questions for, else None.

    The board token comes from the sourcing tag (``source="greenhouse:<slug>"``)
    when present — it's authoritative — with the URL as fallback for jobs that
    arrived via aggregators.
    """
    url_slug, job_id = parse_greenhouse_job_url(job.url)
    slug = url_slug
    if (job.source or "").startswith("greenhouse:"):
        slug = job.source.split(":", 1)[1] or slug
    if slug and job_id:
        return slug, job_id
    return None


def parse_greenhouse_questions(payload: dict[str, Any]) -> list[str]:
    """Free-text custom questions from a Greenhouse job-detail response.

    Keeps a question only if it has a label, at least one free-text field, and no
    boilerplate identity/file field. Select-type questions are dropped (the human
    clicks those). Deduplicates while preserving board order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for q in payload.get("questions") or []:
        if not isinstance(q, dict):
            continue
        label = " ".join(str(q.get("label") or "").split())
        fields = [f for f in (q.get("fields") or []) if isinstance(f, dict)]
        if not label or not fields:
            continue
        names = {str(f.get("name") or "") for f in fields}
        if names & _STANDARD_FIELD_NAMES:
            continue
        types = {str(f.get("type") or "") for f in fields}
        if not (types & _TEXT_TYPES):
            continue
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def fetch_greenhouse_questions(
    client: httpx.Client, *, slug: str, job_id: str, max_retries: int = 3
) -> list[str]:
    """Fetch one job's real free-text questions from the public Greenhouse board API."""
    payload = get_json(
        client,
        GREENHOUSE_JOB_URL.format(slug=slug, job_id=job_id),
        params={"questions": "true"},
        max_retries=max_retries,
    )
    return parse_greenhouse_questions(payload)

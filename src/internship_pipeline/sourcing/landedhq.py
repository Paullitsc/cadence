"""LandedHQ job-tracker puller (landedhq.dev/job-tracker) — Supabase-backed.

Unlike every other sourcing module, this feed sits behind a real signed-in
account: Supabase Row-Level-Security returns zero rows to anonymous requests
(confirmed live — HTTP 200, ``content-range: */0``), so a valid email+password
is mandatory. For that reason this source is OFF by default and meant to stay
local-only: set ``LANDEDHQ_EMAIL``/``LANDEDHQ_PASSWORD`` in your own local
``.env`` (never GitHub Secrets) per ACTIONS_FOR_PAUL.md — the daily CI run has
no way to pick this up unless you deliberately add the secrets there yourself.

The public anon key below is read directly out of LandedHQ's own client-side JS
bundle — standard for Supabase apps (access is enforced by RLS server-side, not
by hiding this key) — and is what the frontend uses for both sign-in and the
`jobs` query. The `jobs` row shape was likewise confirmed by reading the site's
own bundled row-mapping code, not guessed: ``id``, ``company``, ``role``,
``datePosted`` (ISO string), ``applyUrl``, ``type`` (SWE/PM/DS-ML/Quant),
``category`` ("Internship"/"New Grad"). There is no location column in this
schema — # VERIFY if LandedHQ ever adds one — so ``Job.locations`` is left
empty here, and ``is_canadian_job`` naturally defaults to False for these roles.

Same split as every sourcing module: ``parse_landedhq`` is pure (no I/O,
fixture-tested); ``fetch_landedhq`` does the HTTP (sign in, then query).
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..logging_config import get_logger
from ..models import Job, JobSource
from .http import get_json, post_json

log = get_logger(__name__)

LANDEDHQ_SUPABASE_URL = "https://qppemgzmotjsuvcnitzy.supabase.co"
# Public anon key embedded in LandedHQ's own frontend bundle (safe to publish —
# see module docstring).
LANDEDHQ_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFwcGVtZ3ptb3Rqc3V2Y25pdHp5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTQ2MjQwNTUsImV4cCI6MjA3MDIwMDA1NX0."
    "_HZcTDJJDHSHGwD2K7bY3yyAOWdgnwognUGxgflxKoM"
)

_INTERNSHIP_CATEGORY = "Internship"  # the only other observed value is "New Grad" — out of scope


def parse_landedhq(rows: list[dict[str, Any]]) -> list[Job]:
    """Extract internship-only jobs from raw ``jobs`` table rows (pure, no I/O)."""
    jobs: list[Job] = []
    for r in rows or []:
        if (r.get("category") or _INTERNSHIP_CATEGORY) != _INTERNSHIP_CATEGORY:
            continue
        company = r.get("company")
        role = r.get("role")
        url = r.get("applyUrl") or r.get("applyurl")
        if not company or not role or not url:
            continue
        jobs.append(
            Job(
                company_name=company,
                title=role,
                url=url,
                date_posted=r.get("datePosted") or r.get("dateposted"),
                active=True,  # the tracker only lists currently-open roles
                source="landedhq",
                source_feed=JobSource.LANDEDHQ,
            )
        )
    return jobs


def _sign_in(client: httpx.Client, *, email: str, password: str, max_retries: int) -> Optional[str]:
    """Supabase password-grant sign-in -> an access token, or None on bad creds."""
    try:
        data = post_json(
            client,
            f"{LANDEDHQ_SUPABASE_URL}/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
            headers={"apikey": LANDEDHQ_ANON_KEY},
            max_retries=max_retries,
        )
    except httpx.HTTPStatusError as exc:
        log.warning(
            "landedhq sign-in rejected; check LANDEDHQ_EMAIL/LANDEDHQ_PASSWORD",
            extra={"status": exc.response.status_code},
        )
        return None
    return data.get("access_token")


def fetch_landedhq(
    client: httpx.Client, *, email: str, password: str, max_retries: int = 3
) -> list[Job]:
    token = _sign_in(client, email=email, password=password, max_retries=max_retries)
    if not token:
        return []
    rows = get_json(
        client,
        f"{LANDEDHQ_SUPABASE_URL}/rest/v1/jobs",
        params={"select": "*", "order": "datePosted.desc"},
        headers={"apikey": LANDEDHQ_ANON_KEY, "Authorization": f"Bearer {token}"},
        max_retries=max_retries,
    )
    return parse_landedhq(rows)

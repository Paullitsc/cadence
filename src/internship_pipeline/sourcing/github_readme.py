"""Curated GitHub README internship-table puller.

Some curated internship repos publish no JSON feed — just a markdown table in the
README. Format confirmed against the live repo on 2026-07-07
(``negarprh/Canadian-Tech-Internships-2026``, both ``README.md`` and
``README-2027.md``): header ``| Company | Role | Location | Apply | Date Posted |``,
an ``↳`` company cell meaning "same company as the row above", and the apply link
wrapped in a badge — ``[![Apply](badge.svg)](https://real-url)``.

Same split as every sourcing module: ``parse_readme_internships`` is pure (no I/O,
fixture-tested); ``fetch_readme_internships`` does the HTTP. Dates like
``Jun 26, 2026`` are normalized to ISO (``2026-06-26``) so the Phase 4 recency
trigger can read them; anything unparseable is kept raw (→ not treated as recent).
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..logging_config import get_logger
from ..models import Job, JobSource
from .http import get_text
from .util import repo_slug

log = get_logger(__name__)

# Any markdown link target: the LAST one in an Apply cell is the outer apply URL
# (the badge image URL comes first inside `[![Apply](badge)](url)`).
_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
# `[text](url)` → text, for cells that wrap the company/role in a link.
_MD_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")

# Header cells we need to locate (lowercased). "Role" is the confirmed column name;
# "title" accepted as a synonym so a renamed column doesn't silently zero the feed.
_COMPANY = "company"
_ROLE_NAMES = ("role", "title")
_LOCATION = "location"
_APPLY = "apply"
_DATE = "date posted"

_CONTINUATION = "↳"  # "same company as the row above"


def _clean_cell(cell: str) -> str:
    """Plain text of a table cell: unwrap `[text](url)`, strip emphasis markers."""
    text = _MD_LINK_TEXT_RE.sub(r"\1", cell)
    return text.replace("**", "").replace("`", "").strip().strip("*").strip()


def _is_separator(cells: list[str]) -> bool:
    """True for the `|---|:---:|` row under a table header."""
    return all(set(c) <= set(":- ") for c in cells)


def _parse_date(raw: str) -> str | None:
    """``Jun 26, 2026`` → ``2026-06-26``; unknown formats kept raw; blank → None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%b %d, %Y").date().isoformat()
    except ValueError:
        return raw


def parse_readme_internships(markdown: str, *, source: str) -> list[Job]:
    """Extract jobs from every internship table in a README (pure, no I/O).

    A table qualifies when its header row has Company, Role (or Title) and Apply
    columns; other tables in the document are ignored. Rows without an apply URL
    are skipped — without a link there is nothing to dedupe on or apply to.
    """
    jobs: list[Job] = []
    columns: dict[str, int] | None = None
    last_company = ""

    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        lowered = [c.lower() for c in cells]

        if _COMPANY in lowered and _APPLY in lowered and any(r in lowered for r in _ROLE_NAMES):
            columns = {name: i for i, name in enumerate(lowered)}
            last_company = ""
            continue
        if columns is None or _is_separator(cells):
            continue

        def cell(*names: str) -> str:
            for name in names:
                idx = columns.get(name)
                if idx is not None and idx < len(cells):
                    return cells[idx]
            return ""

        company = _clean_cell(cell(_COMPANY))
        if company == _CONTINUATION:
            company = last_company
        title = _clean_cell(cell(*_ROLE_NAMES))
        links = _LINK_RE.findall(cell(_APPLY))
        url = links[-1] if links else None
        if not company or not title or not url:
            continue
        last_company = company

        locations = [p.strip() for p in _clean_cell(cell(_LOCATION)).split("/") if p.strip()]
        jobs.append(
            Job(
                company_name=company,
                title=title,
                url=url,
                locations=locations,
                date_posted=_parse_date(cell(_DATE)),
                active=True,  # these lists only carry open roles; closed ones are removed
                source=source,
                source_feed=JobSource.GITHUB_README,
            )
        )
    return jobs


def fetch_readme_internships(
    client: httpx.Client, url: str, *, source: str | None = None, max_retries: int = 3
) -> list[Job]:
    text = get_text(client, url, max_retries=max_retries)
    return parse_readme_internships(text, source=source or repo_slug(url))

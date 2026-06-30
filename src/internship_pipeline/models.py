"""Shared, typed domain models.

Only fields the blueprint actually specifies are modeled here. Schemas the
blueprint leaves open (outreach, applications, raw ATS responses) are defined by
their phase, to avoid inventing a contract (anti-hallucination rule).
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from .config import Settings, get_settings


class JobSource(str, Enum):
    """Where a job record originated. Values map to blueprint sourcing feeds."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SIMPLIFY = "simplify"  # SimplifyJobs listings.json
    JSEARCH = "jsearch"  # RapidAPI aggregator (optional)


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedupe.

    Lowercases scheme + host, drops the fragment, and strips a trailing slash.
    Path and query are preserved (they often identify the specific posting).
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


class Job(BaseModel):
    """A normalized job posting.

    Fields mirror the SimplifyJobs ``listings.json`` schema named in the blueprint
    (company_name, title, locations, url, date_posted, active, source). ATS feeds
    (Greenhouse/Lever/Ashby) are normalized into this shape in Phase 1.
    """

    company_name: str
    title: str
    url: str
    locations: list[str] = Field(default_factory=list)
    # VERIFY: confirm listings.json `date_posted` format (unix epoch vs ISO string)
    # against the live feed in Phase 1. Kept as raw text here; parsed later.
    date_posted: Optional[str] = None
    active: bool = True
    source: Optional[str] = None
    source_feed: Optional[JobSource] = None

    @field_validator("locations", mode="before")
    @classmethod
    def _coerce_locations(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("date_posted", mode="before")
    @classmethod
    def _coerce_date_posted(cls, v: object) -> object:
        return None if v is None else str(v)

    def dedupe_key(self) -> str:
        """Stable dedupe key (blueprint: dedupe by URL/hash)."""
        digest = hashlib.sha256(normalize_url(self.url).encode("utf-8"))
        return digest.hexdigest()[:16]


class RunRecord(BaseModel):
    """One pipeline run, for the ``runs`` table / daily digest."""

    run_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    status: str = "running"  # running | success | partial | failed


@dataclasses.dataclass
class StageResult:
    """Return value of a stage's ``run`` function."""

    name: str
    counts: dict[str, int]
    notes: str = ""
    ok: bool = True


@dataclasses.dataclass
class StageContext:
    """Shared state passed to each stage."""

    run_id: str
    settings: Settings = dataclasses.field(default_factory=get_settings)
    data: dict = dataclasses.field(default_factory=dict)

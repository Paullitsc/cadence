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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from .config import Settings, get_settings

# Query params that identify a TRACKING SOURCE, not the posting itself — the same
# job appears with/without these depending on which feed surfaced it (e.g.
# SimplifyJobs appends ?utm_source=Simplify&ref=Simplify to the same Greenhouse
# URL a company's own board serves bare). Stripped so dedupe collapses the
# duplicate instead of storing (and re-tailoring) it twice. Deliberately narrow —
# an identity param like Greenhouse's ``gh_jid`` must never be stripped.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    """
    utm_source utm_medium utm_campaign utm_term utm_content utm_id
    ref ref_source referrer source src gh_src lever-origin lever-source
    fbclid gclid mc_cid mc_eid igshid
    """.split()
)


class JobSource(str, Enum):
    """Where a job record originated. Values map to blueprint sourcing feeds."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SIMPLIFY = "simplify"  # SimplifyJobs-format listings.json (incl. forks, e.g. vanshb03)
    JSEARCH = "jsearch"  # RapidAPI aggregator (optional)
    GITHUB_README = "github_readme"  # curated README internship tables (no JSON feed)
    LANDEDHQ = "landedhq"  # landedhq.dev/job-tracker (curated, but account-gated — local-only)


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedupe.

    Lowercases scheme + host, drops the fragment, strips a trailing slash, and
    drops known tracking params (``_TRACKING_PARAMS``) so the same posting
    surfaced by different feeds — one bare, one with ``?utm_source=...`` —
    collapses to one dedupe key. Other query params (often the posting's own
    identity, e.g. ``gh_jid``) are preserved in their original order.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in _TRACKING_PARAMS]
    )
    return urlunsplit((scheme, netloc, path, query, ""))


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
    # Full job-description text. The blueprint's Phase 2 matching step embeds the
    # JD; Phase 1 sourcing does not capture it, so this is optional and Phase 2
    # falls back to title/company text when it is absent.
    description: Optional[str] = None

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


class Application(BaseModel):
    """A prepared (never auto-submitted) application, for the ``applications`` table.

    Phase 2 owns this shape (blueprint: "store tailored resume + drafted common
    answers per job in the tracker"). Keyed by the job's ``dedupe_key`` so each job
    has at most one application row. Everything stays ``pending_review`` — the
    system prepares; the human submits.
    """

    dedupe_key: str  # == Job.dedupe_key(); links back to the jobs table
    company_name: str
    title: str
    url: str
    fit_score: float = 0.0
    keywords: list[str] = Field(default_factory=list)
    tailored_resume_path: Optional[str] = None  # rendered PDF (or None if not rendered)
    tailored_resume_yaml: Optional[str] = None  # cv-doc YAML (auditable source)
    # Phase 5: the DURABLE CV artifact. Local paths die with the CI runner; the Drive
    # link is what the Google Sheet shows. Grouped jobs (similar JDs) share one link.
    cv_drive_link: Optional[str] = None
    drafted_answers: dict[str, str] = Field(default_factory=dict)  # question -> answer
    # CV review workflow: the AI recommends which experience/project bullets to
    # keep; the human confirms/adjusts in the review app. Each entry is
    # {"id": <BulletRef id>, "text": <final bullet text>} in priority order.
    recommended_bullets: list[dict[str, str]] = Field(default_factory=list)
    final_bullets: list[dict[str, str]] = Field(default_factory=list)  # set on review submit
    reviewed_at: Optional[str] = None  # ISO timestamp of the human's review submit
    human_review: bool = False  # high-priority role → flagged for a closer human look
    # pending_review -> reviewed (human finalized the CV in the review app; only
    # then does the row reach the tracker sheet) | expired | rejected/withdrawn
    # (human set the sheet row's Status dropdown — the sync removed the row and
    # recorded that status, which keeps it from ever coming back). Never
    # auto-submitted.
    status: str = "pending_review"


def make_outreach_id(dedupe_key: str, channel: str) -> str:
    """Stable id for one outreach draft: one email + one LinkedIn note per job.

    Deterministic (not random) so re-running the pipeline upserts the same rows
    instead of piling up duplicates — the stage stays idempotent, and the
    ``approve-and-send <outreach_id>`` command has a stable handle.
    """
    return f"{dedupe_key}-{channel}"


class Outreach(BaseModel):
    """A drafted, never-auto-sent cold-outreach message, for the ``outreach`` table.

    Phase 3 owns this shape (assignment #3: contact, channel, draft body, status,
    suppression flag). One row per (job, channel). ``channel="email"`` is the only
    channel with a send path — and only behind the manual ``approve-and-send``
    command. ``channel="linkedin"`` is DRAFT-ONLY: LinkedIn automation/scraping is a
    ban-risk red zone (blueprint finding #6); the system never sends it.
    """

    outreach_id: str  # == make_outreach_id(dedupe_key, channel)
    dedupe_key: str  # == Job.dedupe_key(); links back to the jobs/applications tables
    company_name: str
    title: str
    url: str
    channel: str  # "email" | "linkedin"

    # Contact (assignment #1). For a pattern GUESS, verified=False and confidence is
    # None — we never present a guessed address as certain.
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_title: Optional[str] = None  # recipient's role/position, if known
    contact_source: str = "none"  # "hunter" | "apollo" | "pattern_guess" | "none"
    contact_confidence: Optional[int] = None  # provider confidence 0-100 (None if guessed)
    contact_verified: bool = False
    contact_note: Optional[str] = None  # e.g. "guessed from domain — verify before sending"

    subject: Optional[str] = None  # email only
    body: str = ""  # for email this INCLUDES the CAN-SPAM footer that will be sent
    # Lifecycle: pending_review -> (gmail_draft_created) -> sent -> replied; also
    # approved | suppressed | failed. gmail_draft_created (Phase 5) means a REAL Gmail
    # draft exists for the human to edit + send — still never auto-sent.
    status: str = "pending_review"
    suppressed: bool = False  # contact is on the suppression list → blocked from send
    human_review: bool = False
    used_llm: bool = False

    sent_at: Optional[str] = None
    provider_message_id: Optional[str] = None  # Gmail message id after a successful send
    # Phase 5: the Gmail draft created for this outreach (edit + send from Gmail).
    gmail_draft_id: Optional[str] = None
    gmail_draft_link: Optional[str] = None


class CvCacheEntry(BaseModel):
    """One reusable tailored CV, for the ``cv_cache`` table (Phase 5 cost saver).

    Keyed by a stable hash of (selected bullet ids + normalized keyword set) — see
    ``resume.grouping.cv_cache_key``. If a future run produces the same key, the
    stored CV is reused outright (no LLM tailoring call, no render, no re-upload),
    which catches cross-run duplicates the way within-run clustering catches same-run
    ones.
    """

    cache_key: str
    tailored_resume_yaml: str  # the auditable CV source, re-renderable for free
    cv_drive_link: Optional[str] = None  # durable artifact (None on Drive-less local runs)
    drive_file_id: Optional[str] = None
    pdf_path: Optional[str] = None  # local artifact from the run that built it
    # The selection this CV was tailored from ({"id", "text"} per bullet, priority
    # order) — reused as the recommendation for every application that reuses the
    # cached CV, so the review app can precheck it without re-tailoring.
    recommended_bullets: list[dict[str, str]] = Field(default_factory=list)


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
    """Return value of a stage's ``run`` function.

    ``ok`` is distinct from raising: a stage that catches its own per-item
    errors (e.g. one bad feed) still returns normally, so an exception alone
    can't signal "this stage's OUTCOME was degraded". Set ``ok=False`` when a
    stage completes but accomplished materially less than it should have
    (e.g. every configured source failed) — ``run_pipeline`` records that in
    ``RunRecord.errors`` the same as a raised exception, so it surfaces in the
    run status without aborting the run.
    """

    name: str
    counts: dict[str, int]
    notes: str = ""
    ok: bool = True


# ctx.data keys shared across stages (source -> match_and_slice -> draft_outreach
# -> sync_tracker -> log_and_digest). Centralized so a rename/typo is one edit
# instead of a silent `dict.get(..., default)` miss scattered across files.
DATA_NEW_JOBS = "new_jobs"  # source -> match_and_slice, log_and_digest
DATA_JOBS_TOTAL = "jobs_total"  # source -> log_and_digest
DATA_PREPARED = "prepared"  # match_and_slice -> draft_outreach, sync_tracker
DATA_RESUME = "resume"  # match_and_slice -> draft_outreach (skip a reload)
DATA_LLM_CALLS_SAVED = "llm_calls_saved"  # match_and_slice -> log_and_digest
DATA_OUTREACH = "outreach"  # draft_outreach (currently unread downstream; kept for parity)
DATA_TRACKER_ROWS_REMOVED = "tracker_rows_removed"  # sync_tracker -> log_and_digest


@dataclasses.dataclass
class StageContext:
    """Shared state passed to each stage."""

    run_id: str
    settings: Settings = dataclasses.field(default_factory=get_settings)
    data: dict = dataclasses.field(default_factory=dict)
    # Lazily-created, run-shared Storage — see get_storage(). Not constructed
    # eagerly so a stage-less/no-persist context never opens a connection.
    storage: Optional[object] = None

    def get_storage(self):
        """This run's shared Storage backend, creating it on first use.

        Stages call this instead of ``storage.get_storage(settings)`` directly
        so one run opens ONE backend connection (one Supabase httpx.Client, not
        six) instead of every stage independently constructing and closing its
        own. ``run_pipeline`` closes it once, after every stage has run — a
        stage must never close it itself. Local import: storage/base.py imports
        from this module, so importing Storage at module level here would cycle.
        """
        if self.storage is None:
            from .storage import get_storage as _build_storage

            self.storage = _build_storage(self.settings)
        return self.storage

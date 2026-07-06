"""Runtime configuration loaded from environment / .env (pydantic-settings).

All secrets are optional so the Phase 0 skeleton runs with no credentials. Each
secret is wired in by the phase that needs it (see ACTIONS_FOR_PAUL.md).
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Settings(BaseSettings):
    """Typed application settings. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Core (Phase 0+) ---
    log_level: str = "INFO"
    storage_backend: Literal["sqlite", "supabase"] = "sqlite"
    database_path: str = "data/pipeline.db"
    companies_file: str = "companies.yaml"

    # --- Phase 1: sourcing ---
    # SimplifyJobs raw listings.json (path/branch confirmed against the live repo).
    enable_simplify: bool = True
    simplify_listings_url: str = (
        "https://raw.githubusercontent.com/SimplifyJobs/"
        "Summer2026-Internships/dev/.github/scripts/listings.json"
    )
    # Optional tertiary aggregator (JSearch / RapidAPI). Off by default; the free
    # tier is hard-capped at 200 req/month, so keep page counts tiny.
    enable_jsearch: bool = False
    rapidapi_key: Optional[str] = None  # required only when enable_jsearch=True
    jsearch_host: str = "jsearch.p.rapidapi.com"
    jsearch_query: str = "software engineer intern"
    jsearch_pages: int = 1

    # HTTP + digest
    http_timeout: float = 20.0
    http_max_retries: int = 3
    digest_dir: str = "data/digests"

    # --- Phase 2: résumé slicing + application drafting ---
    # Tagged master résumé (single source of truth). Placeholder content ships so
    # the pipeline runs with zero setup; Paul replaces it with his real résumé.
    master_resume_file: str = "master_resume.yaml"
    # Embedding backend for job↔bullet matching. sentence-transformers is the
    # default (local, free); falls back to a deterministic hashing embedder if the
    # library is not installed, so the pipeline always runs offline.
    embedding_backend: Literal["sentence_transformers", "hashing"] = "sentence_transformers"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Anthropic model for tailoring/answers. Blueprint pins Haiku 4.5; kept as a
    # constant so the model can be swapped without touching code. VERIFY the id in
    # the Anthropic Console if it differs (ACTIONS_FOR_PAUL.md).
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_max_tokens: int = 2048
    # Retrieval / scoring knobs (tune to taste — see ACTIONS_FOR_PAUL.md).
    top_k_bullets: int = 8
    max_tailored_bullets: int = 10  # keep the tailored résumé to ~one page
    fit_score_threshold: float = 0.30  # below this, don't prepare an application
    high_priority_threshold: float = 0.55  # at/above this, flag human_review
    # Cost/volume guard: prepare at most this many applications per run, BEST-fit
    # first. Scoring (embeddings, local) still covers every new job; only the top-N
    # go through LLM tailoring + PDF render + answer/outreach drafting. Protects the
    # first live run (~1,200+ new jobs on day one) from an LLM/render blowout.
    max_applications_per_run: int = 15
    # Comma-separated company names that always count as high-priority (dual-trigger
    # favorable-role flag). Parsed via ``target_company_set``.
    target_companies: str = ""
    resume_output_dir: str = "data/resumes"

    # --- Phase 3: cold outreach ---
    # Contact lookup providers. Both OFF by default; the pipeline always falls back
    # to a company email-pattern GUESS (unverified, never claimed as certain). Free
    # tiers are small (Hunter ~25-50/mo, Apollo ~100 credits/mo), so paid lookups are
    # reserved for high-priority roles and hard-capped per run (see below).
    enable_hunter: bool = False
    hunter_api_key: Optional[str] = None
    enable_apollo: bool = False
    apollo_api_key: Optional[str] = None
    # Cap on billable Hunter/Apollo lookups per run — the free-tier guard. With a
    # daily run this bounds monthly spend well under the free tiers; everything above
    # the cap (and every non-priority role) uses the free pattern guess.
    outreach_max_lookups_per_run: int = 5
    # Only spend a paid lookup on roles already flagged high-priority (human_review).
    outreach_paid_lookup_high_priority_only: bool = True

    # CAN-SPAM footer identity (blueprint + assignment: honest sender identity, a real
    # physical mailing address, and a working opt-out). The send path REFUSES to send
    # while the address is still the REPLACE_ME placeholder — no non-compliant email.
    outreach_from_name: str = ""
    outreach_from_email: Optional[str] = None  # the address shown/used as sender
    outreach_physical_address: str = "REPLACE_ME — your physical mailing address (CAN-SPAM requires it)"
    outreach_opt_out: str = ""  # opt-out text; defaults to "reply STOP to opt out" if blank
    # Optional seed file of emails/domains to never contact (one per line). Merged
    # with the DB-backed suppression list. Blank = DB only.
    outreach_suppression_file: Optional[str] = None

    # Gmail send (Phase 3). Send is behind the manual ``approve-and-send`` command and
    # NEVER runs automatically. Point GMAIL_OAUTH_TOKEN_JSON at the authorized-user
    # token file minted once via ``python -m internship_pipeline.outreach.gmail_auth``
    # (which reads the OAuth client secrets at GMAIL_CREDENTIALS_JSON). See ACTIONS_FOR_PAUL.md.
    gmail_oauth_token_json: Optional[str] = None  # path to authorized-user token JSON
    gmail_credentials_json: Optional[str] = None  # path to OAuth client-secrets JSON (one-time auth)
    gmail_send_scope: str = "https://www.googleapis.com/auth/gmail.send"
    # Phase 4 reply-scan needs read access. The token must be minted with BOTH scopes
    # (gmail_auth requests the full ``gmail_scopes`` list below).
    gmail_read_scope: str = "https://www.googleapis.com/auth/gmail.readonly"

    # --- Phase 4: dual-trigger, digest email, reply scan, alerts, dry-run ---
    # Dual-trigger = enqueue BOTH a prepared application AND a drafted outreach message
    # for a role that is high-fit AND "favorable". A role is favorable when it's a target
    # company OR was posted within ``favorable_recent_days`` (an honest "act soon" proxy —
    # the free feeds don't expose a hard application deadline). ``high_priority_threshold``
    # is the high-fit bar (shared with human_review flagging). All thresholds configurable.
    favorable_recent_days: int = 7

    # Morning digest email (Gmail). Sending the digest TO YOURSELF is safe to automate, so
    # this is the one outbound action the daily run may perform — gated on creds + this flag.
    digest_top_n: int = 10  # top-N applications by fit score shown in the digest
    digest_email_enabled: bool = False  # off by default → digest is written to file only
    digest_to_email: Optional[str] = None  # recipient; defaults to outreach_from_email (self)
    # Recruiter-reply scan: a best-effort Gmail search surfacing recent inbound messages to
    # review. Heuristic (not thread-precise) — labeled as such in the digest. Empty = default.
    reply_scan_days: int = 14
    reply_scan_query: str = ""  # extra Gmail search terms appended to the default
    reply_scan_max: int = 25

    # Failure alert (Phase 4 assignment: implement one channel, stub the other).
    alert_channel: Literal["email", "slack"] = "email"  # chosen: email (Gmail)
    slack_webhook_url: Optional[str] = None  # only used by the stubbed slack channel

    # End-to-end dry-run: exercise every stage from bundled fixtures with zero live creds.
    dry_run: bool = False
    dry_run_jobs_file: Optional[str] = None  # None → bundled fixtures/dry_run_jobs.json

    # --- Secrets (optional until their phase) ---
    anthropic_api_key: Optional[str] = None  # Phase 2+
    supabase_url: Optional[str] = None  # Phase 1+ (if storage_backend=supabase)
    supabase_key: Optional[str] = None

    @property
    def target_company_set(self) -> set[str]:
        """Lowercased set of always-high-priority company names."""
        return {c.strip().lower() for c in self.target_companies.split(",") if c.strip()}

    @property
    def gmail_scopes(self) -> list[str]:
        """All Gmail scopes the app uses (send for outreach, readonly for reply scan)."""
        return [self.gmail_send_scope, self.gmail_read_scope]

    @property
    def digest_recipient(self) -> Optional[str]:
        """Who the digest email goes to — defaults to the sender (yourself)."""
        return (self.digest_to_email or self.outreach_from_email or "").strip() or None


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def build_dry_run_settings(*, work_dir: Optional[str] = None) -> Settings:
    """Fully self-contained settings for ``--dry-run``: bundled fixtures, temp SQLite,
    deterministic embedder, and every external source / credential disabled.

    Thresholds are relaxed and a fixture company is targeted so at least one role fires
    the dual-trigger — exercising the outreach path — while the others stay
    application-only, so a single run touches every branch with zero live creds.
    """
    base = Path(work_dir or tempfile.mkdtemp(prefix="internship-dry-run-"))
    return Settings(
        _env_file=None,
        dry_run=True,
        dry_run_jobs_file=str(_FIXTURES / "dry_run_jobs.json"),
        storage_backend="sqlite",
        database_path=str(base / "pipeline.db"),
        master_resume_file=str(_FIXTURES / "dry_run_resume.yaml"),
        embedding_backend="hashing",  # deterministic, no model download
        enable_simplify=False,
        enable_jsearch=False,
        enable_hunter=False,
        enable_apollo=False,
        anthropic_api_key=None,  # deterministic tailoring / no answer drafting
        gmail_oauth_token_json=None,  # no send, no reply scan
        digest_email_enabled=False,
        digest_dir=str(base / "digests"),
        resume_output_dir=str(base / "resumes"),
        target_companies="Dry Run Labs",  # → one favorable role → dual-trigger
        fit_score_threshold=0.0,
        high_priority_threshold=0.0,
        outreach_from_name="Dry Run Candidate",
        outreach_from_email="candidate@example.com",
        outreach_physical_address="123 Example St, Remoteville",
    )

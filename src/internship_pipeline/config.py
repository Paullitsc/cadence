"""Runtime configuration loaded from environment / .env (pydantic-settings).

All secrets are optional so the Phase 0 skeleton runs with no credentials. Each
secret is wired in by the phase that needs it (see ACTIONS_FOR_PAUL.md).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Secrets (optional until their phase) ---
    anthropic_api_key: Optional[str] = None  # Phase 2+
    supabase_url: Optional[str] = None  # Phase 1+ (if storage_backend=supabase)
    supabase_key: Optional[str] = None

    @property
    def target_company_set(self) -> set[str]:
        """Lowercased set of always-high-priority company names."""
        return {c.strip().lower() for c in self.target_companies.split(",") if c.strip()}


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()

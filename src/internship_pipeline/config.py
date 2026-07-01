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

    # --- Secrets (optional until their phase) ---
    anthropic_api_key: Optional[str] = None  # Phase 2+
    supabase_url: Optional[str] = None  # Phase 1+ (if storage_backend=supabase)
    supabase_key: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()

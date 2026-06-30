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

    # --- Secrets (optional until their phase) ---
    anthropic_api_key: Optional[str] = None  # Phase 2+
    supabase_url: Optional[str] = None  # Phase 1+ (if storage_backend=supabase)
    supabase_key: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()

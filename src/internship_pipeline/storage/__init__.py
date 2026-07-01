"""Storage backends + factory.

Supabase (Postgres) is primary; SQLite is the local-dev fallback. The factory
falls back to SQLite if ``storage_backend=supabase`` but the URL/key are missing,
so a misconfigured run degrades gracefully instead of crashing.
"""

from __future__ import annotations

from ..config import Settings
from ..logging_config import get_logger
from .base import Storage, UpsertResult, chunked
from .sqlite_store import SQLiteStore
from .supabase_store import SupabaseStore

log = get_logger(__name__)

__all__ = [
    "Storage",
    "UpsertResult",
    "chunked",
    "SQLiteStore",
    "SupabaseStore",
    "get_storage",
]


def get_storage(settings: Settings) -> Storage:
    """Construct the configured storage backend."""
    if settings.storage_backend == "supabase":
        if settings.supabase_url and settings.supabase_key:
            return SupabaseStore(
                settings.supabase_url,
                settings.supabase_key,
                timeout=settings.http_timeout,
            )
        log.warning(
            "storage_backend=supabase but SUPABASE_URL/KEY missing; "
            "falling back to sqlite",
            extra={"database_path": settings.database_path},
        )
    return SQLiteStore(settings.database_path)

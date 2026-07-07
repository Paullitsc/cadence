"""Storage interface shared by the SQLite (local) and Supabase (primary) backends.

Dedupe is by stable job hash (``Job.dedupe_key``). The "new jobs today" delta is
computed by diffing the incoming keys against ``existing_keys`` BEFORE insert, so
it is backend-agnostic and deterministic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Optional

from ..models import Application, CvCacheEntry, Job, Outreach, RunRecord


@dataclass
class UpsertResult:
    """Outcome of an ``upsert_jobs`` call."""

    new: list[Job] = field(default_factory=list)  # rows inserted this run
    seen: int = 0  # rows already present (last_seen bumped)

    @property
    def new_count(self) -> int:
        return len(self.new)


def chunked(items: list[str], size: int) -> Iterator[list[str]]:
    """Yield ``items`` in lists of at most ``size`` (for IN-clause batching)."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Storage(ABC):
    """Persistence for the ``jobs`` and ``runs`` tables."""

    @abstractmethod
    def existing_keys(self, keys: Iterable[str]) -> set[str]:
        """Return the subset of ``keys`` (dedupe keys) already stored."""

    @abstractmethod
    def upsert_jobs(self, jobs: list[Job]) -> UpsertResult:
        """Insert new jobs, bump ``last_seen_at`` on ones already stored."""

    @abstractmethod
    def record_run(self, run: RunRecord) -> None:
        """Persist a ``runs`` row for the daily log."""

    @abstractmethod
    def save_application(self, app: Application) -> None:
        """Upsert an ``applications`` row (keyed by ``dedupe_key``)."""

    @abstractmethod
    def get_application(self, dedupe_key: str) -> Optional[Application]:
        """Load a stored application by job dedupe key, or None if absent."""

    @abstractmethod
    def list_applications(self, status: Optional[str] = None) -> list[Application]:
        """List applications, optionally filtered by status (highest fit first)."""

    # --- Phase 3: outreach + suppression list ---

    @abstractmethod
    def save_outreach(self, outreach: Outreach) -> None:
        """Upsert an ``outreach`` row (keyed by ``outreach_id``)."""

    @abstractmethod
    def get_outreach(self, outreach_id: str) -> Optional[Outreach]:
        """Load a stored outreach draft by id, or None if absent."""

    @abstractmethod
    def list_outreach(self, status: Optional[str] = None) -> list[Outreach]:
        """List outreach drafts, optionally filtered by status (newest first)."""

    @abstractmethod
    def add_suppression(self, entry: str, reason: Optional[str] = None) -> None:
        """Add an email address OR a bare domain to the do-not-contact list."""

    @abstractmethod
    def is_suppressed(self, email: str) -> bool:
        """True if this exact email, or its domain, is on the suppression list."""

    @abstractmethod
    def list_suppressions(self) -> list[str]:
        """Return all suppression entries (emails and/or domains), lowercased."""

    # --- Phase 5: cross-run CV cache (reuse a tailored CV, skip the LLM call) ---

    @abstractmethod
    def get_cv_cache(self, cache_key: str) -> Optional[CvCacheEntry]:
        """Load a cached tailored CV by its input-identity key, or None."""

    @abstractmethod
    def save_cv_cache(self, entry: CvCacheEntry) -> None:
        """Upsert a ``cv_cache`` row (keyed by ``cache_key``)."""

    @abstractmethod
    def list_cv_cache(self) -> list[CvCacheEntry]:
        """All cached CVs — small table (one row per unique CV), scanned for
        content-identical duplicates so one rendered CV keeps one Drive link."""

    def close(self) -> None:  # optional; overridden where a client is held open
        pass


def suppression_matches(email: str, entries: Iterable[str]) -> bool:
    """Backend-agnostic suppression check: exact-email OR domain match.

    An entry may be a full address (``a@b.com``) or a bare domain (``b.com``).
    Matching is case-insensitive. Shared by both backends so the rule is identical.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    domain = email.split("@", 1)[1] if "@" in email else ""
    for raw in entries:
        entry = (raw or "").strip().lower()
        if not entry:
            continue
        if entry == email:
            return True
        if "@" not in entry and domain and entry == domain:
            return True
    return False

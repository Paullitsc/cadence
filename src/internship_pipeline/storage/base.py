"""Storage interface shared by the SQLite (local) and Supabase (primary) backends.

Dedupe is by stable job hash (``Job.dedupe_key``). The "new jobs today" delta is
computed by diffing the incoming keys against ``existing_keys`` BEFORE insert, so
it is backend-agnostic and deterministic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ..models import Job, RunRecord


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

    def close(self) -> None:  # optional; overridden where a client is held open
        pass

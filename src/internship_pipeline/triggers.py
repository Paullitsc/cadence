"""Dual-trigger logic (Phase 4).

The dual-trigger decides when a role earns BOTH a prepared application *and* a drafted
outreach message (each ``pending_review``): it must be **high-fit** (``fit_score >=
high_priority_threshold``) **AND favorable**. Everything above ``fit_score_threshold``
still gets an application; only dual-trigger roles also get outreach — which keeps the
scarce, sometimes-paid contact lookups aimed at the roles worth the extra push.

"Favorable" = a **target company** OR a role **posted within ``favorable_recent_days``**.
The recency test is an honest proxy for the assignment's "deadline soon": the free feeds
(SimplifyJobs/ATS) don't expose a hard application deadline, so we treat a freshly-posted
role as the actionable "act soon" signal instead of inventing a deadline field.

All pure + deterministic (no I/O), so the whole rule is unit-tested offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import Settings
from .models import Job


@dataclass(frozen=True)
class Favorability:
    """Why (or whether) a role counts as favorable."""

    favorable: bool
    reason: str = ""  # human-readable, e.g. "target company" / "posted 2d ago"


def _parse_posted(date_posted: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a ``Job.date_posted`` (unix epoch or ISO-8601) → aware UTC.

    Feeds are inconsistent (SimplifyJobs uses epoch seconds; ATS feeds use ISO), and the
    field is optional. Anything unparseable returns None (→ not treated as recent).
    """
    if not date_posted:
        return None
    raw = str(date_posted).strip()
    # Epoch seconds (SimplifyJobs). Guard against millisecond epochs too.
    if raw.isdigit():
        ts = int(raw)
        if ts > 10_000_000_000:  # looks like milliseconds
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def posted_within_days(job: Job, days: int, *, now: Optional[datetime] = None) -> bool:
    """True if the role was posted within ``days`` (False if the date is unknown)."""
    if days <= 0:
        return False
    posted = _parse_posted(job.date_posted)
    if posted is None:
        return False
    now = now or datetime.now(timezone.utc)
    return 0 <= (now - posted).total_seconds() <= days * 86400


def favorability(job: Job, settings: Settings, *, now: Optional[datetime] = None) -> Favorability:
    """Decide whether a role is favorable, and say why."""
    if job.company_name.strip().lower() in settings.target_company_set:
        return Favorability(True, "target company")
    if posted_within_days(job, settings.favorable_recent_days, now=now):
        return Favorability(True, f"posted within {settings.favorable_recent_days}d")
    return Favorability(False)


def is_dual_trigger(fit_score: float, favorable: bool, settings: Settings) -> bool:
    """High-fit AND favorable → enqueue both an application and an outreach draft."""
    return favorable and fit_score >= settings.high_priority_threshold

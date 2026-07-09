"""Render + write the daily digest.

Phase 1 rendered "new jobs"; Phase 4 added every pending queue. Phase 5 splits the
two human touchpoints by what they're FOR: the Google Sheet is the application
workspace (per-application rows, CV links, drafted answers, status/notes), so the
digest slims down to what genuinely belongs in an inbox — a compact count header
with one link to the sheet, the outreach drafts (with Gmail-draft links), and the
possible-replies scan. The digest file is still written locally every run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from jinja2 import Environment, FileSystemLoader

from ..models import Job, Outreach

if TYPE_CHECKING:  # avoid importing the Gmail path just for a type hint
    from ..outreach.replies import Reply

_TEMPLATES = Path(__file__).parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        # Always autoescape — this env only renders the HTML digest, and the
        # `.j2` extension isn't matched by select_autoescape(["html"]).
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _digest_context(
    *,
    jobs: list[Job],
    run_id: str,
    generated_at: Optional[datetime],
    counts: Optional[dict[str, int]],
    pending_outreach: Optional[list[Outreach]],
    replies: "Optional[list[Reply]]",
    sheet_url: Optional[str],
) -> dict:
    counts = dict(counts or {})
    counts.setdefault("new", len(jobs))
    return {
        "run_id": run_id,
        "generated_at": generated_at or datetime.now(timezone.utc),
        "counts": counts,
        "pending_outreach": pending_outreach or [],
        "replies": replies or [],
        "sheet_url": sheet_url,
    }


def render_digest(
    *,
    jobs: list[Job],
    run_id: str,
    generated_at: Optional[datetime] = None,
    counts: Optional[dict[str, int]] = None,
    pending_outreach: Optional[list[Outreach]] = None,
    replies: "Optional[list[Reply]]" = None,
    sheet_url: Optional[str] = None,
) -> str:
    """Render the digest HTML."""
    ctx = _digest_context(
        jobs=jobs, run_id=run_id, generated_at=generated_at, counts=counts,
        pending_outreach=pending_outreach, replies=replies, sheet_url=sheet_url,
    )
    return _env().get_template("digest.html.j2").render(**ctx)


def render_digest_text(
    *,
    jobs: list[Job],
    run_id: str,
    counts: Optional[dict[str, int]] = None,
    pending_outreach: Optional[list[Outreach]] = None,
    replies: "Optional[list[Reply]]" = None,
    sheet_url: Optional[str] = None,
) -> str:
    """A short plain-text summary (the alternative part of the digest email)."""
    counts = dict(counts or {})
    counts.setdefault("new", len(jobs))
    lines = [
        f"Internship digest — run {run_id}",
        f"New jobs: {counts.get('new', 0)}",
        f"Applications prepared: {counts.get('applications_prepared', 0)}",
        f"Awaiting your CV review (make review): {counts.get('applications_pending', 0)}",
        f"LLM calls saved (CV grouping): {counts.get('llm_calls_saved', 0)}",
        f"Outreach drafts awaiting approval: {len(pending_outreach or [])}",
        f"Possible replies to review: {len(replies or [])}",
    ]
    if sheet_url:
        lines.append(f"Application tracker: {sheet_url}")
    lines += [
        "",
        "Reviewed applications live in the tracker sheet; pending ones wait in the "
        "review app. Sending/submitting is always done by you.",
    ]
    return "\n".join(lines)


def write_digest(html: str, dir_path: str, *, date: Optional[str] = None) -> Path:
    """Write ``html`` to ``<dir>/digest-YYYYMMDD.html`` and ``<dir>/latest.html``."""
    out_dir = Path(dir_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = date or datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"digest-{date}.html"
    path.write_text(html, encoding="utf-8")
    (out_dir / "latest.html").write_text(html, encoding="utf-8")
    return path

"""Render + write the daily digest.

Phase 1 rendered just "new jobs". Phase 4 turns it into the single morning touchpoint:
new jobs, top matches by fit, outreach drafts awaiting approval, applications prepared
awaiting submit, and possible recruiter replies. All new sections are optional, so the
Phase-1 call site (and its tests) keep working unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from jinja2 import Environment, FileSystemLoader

from ..models import Application, Job, Outreach

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
    top_applications: Optional[list[Application]],
    pending_outreach: Optional[list[Outreach]],
    pending_applications: Optional[list[Application]],
    replies: "Optional[list[Reply]]",
) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc)
    by_company: dict[str, list[Job]] = {}
    for job in sorted(jobs, key=lambda j: (j.company_name.lower(), j.title.lower())):
        by_company.setdefault(job.company_name, []).append(job)
    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "count": len(jobs),
        "counts": counts or {},
        "by_company": by_company,
        "top_applications": top_applications or [],
        "pending_outreach": pending_outreach or [],
        "pending_applications": pending_applications or [],
        "replies": replies or [],
    }


def render_digest(
    *,
    jobs: list[Job],
    run_id: str,
    generated_at: Optional[datetime] = None,
    counts: Optional[dict[str, int]] = None,
    top_applications: Optional[list[Application]] = None,
    pending_outreach: Optional[list[Outreach]] = None,
    pending_applications: Optional[list[Application]] = None,
    replies: "Optional[list[Reply]]" = None,
) -> str:
    """Render the digest HTML."""
    ctx = _digest_context(
        jobs=jobs, run_id=run_id, generated_at=generated_at, counts=counts,
        top_applications=top_applications, pending_outreach=pending_outreach,
        pending_applications=pending_applications, replies=replies,
    )
    return _env().get_template("digest.html.j2").render(**ctx)


def render_digest_text(
    *,
    jobs: list[Job],
    run_id: str,
    counts: Optional[dict[str, int]] = None,
    top_applications: Optional[list[Application]] = None,
    pending_outreach: Optional[list[Outreach]] = None,
    pending_applications: Optional[list[Application]] = None,
    replies: "Optional[list[Reply]]" = None,
) -> str:
    """A short plain-text summary (the alternative part of the digest email)."""
    lines = [
        f"Internship digest — run {run_id}",
        f"New internships today: {len(jobs)}",
        f"Top matches prepared: {len(top_applications or [])}",
        f"Outreach drafts awaiting approval: {len(pending_outreach or [])}",
        f"Applications awaiting submit: {len(pending_applications or [])}",
        f"Possible replies to review: {len(replies or [])}",
        "",
        "Open the HTML digest for details. Sending/submitting is always done by you.",
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

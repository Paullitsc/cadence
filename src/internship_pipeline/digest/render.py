"""Render + write the daily HTML digest of new jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from ..models import Job

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


def render_digest(
    *,
    jobs: list[Job],
    run_id: str,
    generated_at: Optional[datetime] = None,
    counts: Optional[dict[str, int]] = None,
) -> str:
    """Render the digest HTML for the given new ``jobs``."""
    generated_at = generated_at or datetime.now(timezone.utc)
    by_company: dict[str, list[Job]] = {}
    for job in sorted(jobs, key=lambda j: (j.company_name.lower(), j.title.lower())):
        by_company.setdefault(job.company_name, []).append(job)
    template = _env().get_template("digest.html.j2")
    return template.render(
        run_id=run_id,
        generated_at=generated_at,
        count=len(jobs),
        counts=counts or {},
        by_company=by_company,
    )


def write_digest(html: str, dir_path: str, *, date: Optional[str] = None) -> Path:
    """Write ``html`` to ``<dir>/digest-YYYYMMDD.html`` and ``<dir>/latest.html``."""
    out_dir = Path(dir_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = date or datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"digest-{date}.html"
    path.write_text(html, encoding="utf-8")
    (out_dir / "latest.html").write_text(html, encoding="utf-8")
    return path

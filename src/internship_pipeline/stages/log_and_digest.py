"""Stage: log & digest (Phase 1).

Compute "new jobs today" (handed over by the ``source`` stage in
``ctx.data["new_jobs"]``), render an HTML digest with jinja2, and WRITE it to a
local file. Real email sending is intentionally NOT wired up here — that is a
later, human-gated phase (blueprint section 6). The ``runs`` row is persisted by
the orchestrator once the full run finishes.
"""

from __future__ import annotations

from ..digest import render_digest, write_digest
from ..logging_config import get_logger
from ..models import Job, StageContext, StageResult

NAME = "log_and_digest"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})

    new_jobs: list[Job] = ctx.data.get("new_jobs", [])
    counts = {
        "new": len(new_jobs),
        "total_sourced": ctx.data.get("jobs_total", 0),
    }
    html = render_digest(jobs=new_jobs, run_id=ctx.run_id, counts=counts)
    path = write_digest(html, ctx.settings.digest_dir)

    log.info(
        "digest written",
        extra={
            "run_id": ctx.run_id,
            "stage": NAME,
            "new_jobs_today": len(new_jobs),
            "path": str(path),
        },
    )
    return StageResult(
        name=NAME,
        counts={"new_jobs_today": len(new_jobs), "digest_written": 1},
        notes=str(path),
    )


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

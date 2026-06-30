"""Stage: sourcing.

Phase 1 implements this: pull public ATS JSON feeds (Greenhouse/Lever/Ashby) and
the SimplifyJobs ``listings.json``, normalize to ``Job``, dedupe, and persist.
Phase 0 is a stub.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import StageContext, StageResult

NAME = "source"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    log.warning(
        "stub: not implemented in Phase 0",
        extra={"run_id": ctx.run_id, "stage": NAME},
    )
    return StageResult(name=NAME, counts={"jobs_sourced": 0}, notes="stub")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

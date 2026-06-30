"""Stage: log & digest.

Phase 1/4 implements this: write the run's RunRecord row and email a morning HTML
digest (new jobs, top-N by fit, drafts awaiting approval, prepared applications,
detected recruiter replies). Phase 0 is a stub.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import StageContext, StageResult

NAME = "log_and_digest"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    log.warning(
        "stub: not implemented in Phase 0",
        extra={"run_id": ctx.run_id, "stage": NAME},
    )
    return StageResult(name=NAME, counts={"digests_sent": 0}, notes="stub")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

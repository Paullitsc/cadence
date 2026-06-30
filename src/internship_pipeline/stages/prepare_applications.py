"""Stage: prepare applications.

Phase 2-3 implements this: assemble a tailored resume PDF + pre-written answers to
common questions per high-fit job, stored as ``pending_review``. You autofill
(Simplify Copilot) and click submit. Phase 0 is a stub.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import StageContext, StageResult

NAME = "prepare_applications"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    log.warning(
        "stub: not implemented in Phase 0",
        extra={"run_id": ctx.run_id, "stage": NAME},
    )
    return StageResult(name=NAME, counts={"applications_prepared": 0}, notes="stub")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

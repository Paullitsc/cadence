"""Stage: draft outreach.

Phase 3 implements this: look up contacts (Hunter/Apollo), draft a short, specific
cold email / LinkedIn note, and enqueue it as ``pending_review``. Sending is always
human-gated. Phase 0 is a stub.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import StageContext, StageResult

NAME = "draft_outreach"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    log.warning(
        "stub: not implemented in Phase 0",
        extra={"run_id": ctx.run_id, "stage": NAME},
    )
    return StageResult(name=NAME, counts={"outreach_drafted": 0}, notes="stub")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

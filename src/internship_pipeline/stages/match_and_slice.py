"""Stage: match & slice.

Phase 2 implements this: embed JD + resume bullets, score job-to-profile fit,
extract JD keywords, and tailor a one-page resume from REAL bullets only (Haiku,
low temperature), then render a PDF with RenderCV. Phase 0 is a stub.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import StageContext, StageResult

NAME = "match_and_slice"

log = get_logger(__name__)


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    log.warning(
        "stub: not implemented in Phase 0",
        extra={"run_id": ctx.run_id, "stage": NAME},
    )
    return StageResult(name=NAME, counts={"jobs_scored": 0}, notes="stub")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

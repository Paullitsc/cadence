"""Daily pipeline entrypoint.

Runs deterministic stages in order. Each stage is wrapped so that a failure is
logged and skipped — one bad stage must not kill the run. Stages are idempotent
and independently runnable::

    python -m internship_pipeline.run_daily                  # all stages
    python -m internship_pipeline.run_daily --stage source   # one stage

Phase 0 stages are stubs; later phases fill in the logic.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import Settings, get_settings
from .logging_config import configure_logging, get_logger
from .models import RunRecord, StageContext, StageResult
from .stages import (
    draft_outreach,
    log_and_digest,
    match_and_slice,
    prepare_applications,
    source,
)

# Ordered registry of stage name -> run function.
REGISTRY: dict[str, Callable[[StageContext], StageResult]] = {
    source.NAME: source.run,
    match_and_slice.NAME: match_and_slice.run,
    draft_outreach.NAME: draft_outreach.run,
    prepare_applications.NAME: prepare_applications.run,
    log_and_digest.NAME: log_and_digest.run,
}

log = get_logger(__name__)


def run_pipeline(
    stages: Optional[list[str]] = None,
    settings: Optional[Settings] = None,
    persist: bool = True,
) -> RunRecord:
    """Execute the given stages (default: all) and return a RunRecord.

    Does not configure logging (entrypoints do that) so it is safe to call from
    tests and other code without clobbering handlers. When ``persist`` is True the
    final RunRecord is written to the ``runs`` table (best-effort; a storage error
    is logged, not raised).
    """
    settings = settings or get_settings()
    names = stages if stages is not None else list(REGISTRY)
    run_id = uuid.uuid4().hex[:12]
    ctx = StageContext(run_id=run_id, settings=settings)
    record = RunRecord(run_id=run_id, started_at=datetime.now(timezone.utc))

    log.info("run start", extra={"run_id": run_id, "stages": names})
    for name in names:
        fn = REGISTRY.get(name)
        if fn is None:
            record.errors.append(f"unknown stage: {name}")
            log.error("unknown stage", extra={"run_id": run_id, "stage": name})
            continue
        try:
            result = fn(ctx)
            for key, value in result.counts.items():
                record.counts[key] = record.counts.get(key, 0) + value
            log.info(
                "stage done",
                extra={"run_id": run_id, "stage": name, "counts": result.counts},
            )
        except Exception as exc:  # skip-on-error: keep the run going
            record.errors.append(f"{name}: {exc!r}")
            log.exception("stage failed", extra={"run_id": run_id, "stage": name})

    record.finished_at = datetime.now(timezone.utc)
    if names and len(record.errors) >= len(names):
        record.status = "failed"
    elif record.errors:
        record.status = "partial"
    else:
        record.status = "success"

    if persist:
        try:
            from .storage import get_storage

            storage = get_storage(settings)
            try:
                storage.record_run(record)
            finally:
                storage.close()
        except Exception as exc:  # observability must not break the run
            log.warning("failed to persist run record", extra={"run_id": run_id, "error": repr(exc)})

    log.info(
        "run end",
        extra={
            "run_id": run_id,
            "status": record.status,
            "counts": record.counts,
            "errors": record.errors,
        },
    )
    return record


def run_single(name: str) -> int:
    """Run one stage by name (used by ``python -m internship_pipeline.stages.<name>``)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    record = run_pipeline(stages=[name], settings=settings)
    return 0 if record.status == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Internship pipeline daily run.")
    parser.add_argument(
        "--stage",
        action="append",
        choices=list(REGISTRY),
        help="Run only this stage (repeatable). Default: all stages.",
    )
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(args.log_level or settings.log_level)
    record = run_pipeline(stages=args.stage, settings=settings)
    return 0 if record.status in {"success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

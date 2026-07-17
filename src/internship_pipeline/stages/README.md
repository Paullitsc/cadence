# Stages

`run_daily.py` runs these in order. Each module exposes `NAME` and
`run(ctx: StageContext) -> StageResult`, is idempotent, and is runnable on its own
(`python -m internship_pipeline.stages.<name>`).

| Order | Module | `NAME` | Status |
| ----- | ------ | ------ | ------ |
| 1 | `source.py` | `source` | ✅ Phase 1 |
| 2 | `match_and_slice.py` | `match_and_slice` | ⬜ stub (Phase 2) |
| 3 | `draft_outreach.py` | `draft_outreach` | ⬜ stub (Phase 3) |
| 4 | `log_and_digest.py` | `log_and_digest` | ✅ Phase 1 (digest to file; email in Phase 4) |

`source` and `log_and_digest` are implemented; the middle three are Phase 0 stubs
that log and return zero counts. The orchestrator catches a stage exception,
records it, and continues (skip-on-error). `source` hands new jobs to
`log_and_digest` via `ctx.data["new_jobs"]`.

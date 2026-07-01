# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A scheduled, logged, human-in-the-loop internship-hunting pipeline: deterministic
Python orchestration with narrow LLM calls reserved only for reasoning steps
(résumé matching/tailoring, outreach drafting) in later phases. **The design
document `Automated Intern Recruitment Workflow.md` (the "blueprint") is the single
source of truth** — it pins the exact API endpoints, costs, and legal guardrails.

## Working rules specific to this project

- **Build only the phase you're asked to build, then stop.** Work is staged 0→4
  (see README roadmap). Phases 0 and 1 are done; phases 2–4 are deliberate stubs.
  Do not implement a later phase's logic ahead of time.
- **No invented APIs or endpoints.** Use exactly what the blueprint specifies. When
  unsure of an external response field name, prefer confirming it against the live
  feed; if it genuinely can't be verified, mark it `# VERIFY` in code and surface
  it to the user rather than guessing (existing example: JSearch fields in
  `sourcing/jsearch.py`).
- **Humans gate all outbound actions** (sending email, submitting applications).
  The system *prepares*; the user *sends/submits*. Never automate LinkedIn. Résumé
  tailoring (Phase 2) reorders/rephrases only real bullets — never fabricates.
- **Secrets are optional until the phase that needs them** (`config.py`), so the
  pipeline must always run end-to-end with zero credentials.

## Commands

```bash
uv sync --extra dev                               # install (pip fallback: requirements-dev.txt)

uv run pytest                                     # all tests
uv run pytest tests/test_storage.py              # one file
uv run pytest tests/test_ats.py::test_lever_uses_company_name_and_all_locations  # one test
uv run ruff check .                               # lint (also: ruff check --fix .)

uv run python -m internship_pipeline.run_daily               # run all stages
uv run python -m internship_pipeline.run_daily --stage source  # one stage (repeatable)
uv run python -m internship_pipeline.stages.source          # run a stage standalone
```

`make test` / `make lint` / `make run` wrap the above. Target runtime is Python 3.12.

## Architecture

**Orchestrator + stage registry.** `run_daily.py` holds an ordered `REGISTRY` of
stage `NAME -> run(ctx) -> StageResult`. It runs each stage wrapped in try/except
(**skip-on-error**: one failing stage logs and is recorded, the run continues),
aggregates counts into a `RunRecord`, derives `success`/`partial`/`failed`, and
best-effort persists the run row. Every stage module exposes `NAME` + `run`, is
idempotent, and is independently runnable. The five stages in order: `source` →
`match_and_slice` → `draft_outreach` → `prepare_applications` → `log_and_digest`.
Stages share state through `ctx.data` (e.g. `source` puts new jobs in
`ctx.data["new_jobs"]`, which `log_and_digest` reads) — there is no return-value
chaining between stages.

**One normalized model + hash dedupe.** Every source (Greenhouse/Lever/Ashby/
SimplifyJobs/JSearch) is normalized into the single `Job` model (`models.py`).
Dedup key = SHA-256 of the canonicalized URL (`normalize_url`), so the same posting
appearing in multiple feeds, or with a trailing-slash variant, collapses to one row.

**Sourcing split: pure parse vs. network fetch.** In `sourcing/`, each fetcher is
two functions: `parse_<ats>(payload, ...)` (pure, no I/O) and `fetch_<ats>(client,
target)` (does the HTTP). **Tests only exercise the `parse_*` functions against JSON
fixtures in `tests/fixtures/` — no test touches the network.** External calls go
through `sourcing/http.py` (`httpx` + `tenacity`, retrying only transport errors /
429 / 5xx so a bad board token fails fast). `companies.py` loads `companies.yaml`
(schema documented in `companies.example.yaml`) and skips placeholder/invalid rows.

**Storage abstraction with graceful degradation.** `storage/get_storage(settings)`
returns the `Storage` ABC implementation: `SupabaseStore` (primary, talks to
Supabase PostgREST over `httpx` — no SDK dependency) or `SQLiteStore` (local
fallback, self-initializing schema). If `STORAGE_BACKEND=supabase` but creds are
missing, it logs and falls back to SQLite. "New vs. seen" is computed
backend-agnostically by diffing incoming dedup keys against `existing_keys` before
insert. SQL migrations live in `storage/sql/` (`postgres.sql` for Supabase,
`sqlite.sql` mirrors it).

**Config.** `config.py` uses `pydantic-settings`; field names map to UPPER_CASE env
vars / `.env`. `get_settings()` is an lru_cached singleton; tests construct
`Settings(_env_file=None, ...)` to stay deterministic and offline.

**Two GitHub workflows, different jobs.** `.github/workflows/ci.yml` is lint+test on
push/PR. `.github/workflows/daily.yml` is the production cron (`0 13 * * *`) that
runs the pipeline, reads secrets, and uploads the digest artifact — note GitHub
disables scheduled workflows after 60 days of repo inactivity (keep-alive needed).

## Conventions

- Manual (non-code) setup the user must do lives in `ACTIONS_FOR_PAUL.md` — when a
  phase needs an external account/key/dashboard step, add it there with the exact
  secret name rather than doing it in code.
- Modules start with `from __future__ import annotations`; logging is structured
  JSON via `get_logger(__name__)` with context passed as `extra={...}`.
- Add new domain fields to a model only when the blueprint actually specifies the
  contract; otherwise let the phase that owns it define the shape.

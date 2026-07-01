# Automated Internship Workflow

A scheduled, logged, **human-in-the-loop** internship-hunting pipeline:
deterministic Python orchestration with **narrow LLM calls** only where reasoning
helps (matching, résumé tailoring, outreach drafting). The full design and the
validated facts (API endpoints, costs, legal guardrails) live in
[`Automated Intern Recruitment Workflow.md`](./Automated%20Intern%20Recruitment%20Workflow.md)
— that blueprint is the single source of truth.

> **Status: Phase 1 (sourcing + tracking) — complete.** Every run pulls a fresh,
> deduped list of new internships from public ATS feeds + SimplifyJobs (+ optional
> JSearch), stores them (Supabase/SQLite), and writes an HTML digest of "new jobs
> today". Zero LLM cost. Stages 2–4 remain **stubs**, each built in its own phase.

## Phased roadmap

| Phase | Scope | State |
| ----- | ----- | ----- |
| 0 | Repo scaffold, tooling, config, logging, models, stage skeleton, tests | ✅ done |
| 1 | Sourcing + tracking (ATS feeds, SimplifyJobs JSON, dedupe, store, digest) | ✅ done |
| 2 | Résumé slicing + application drafting (embeddings, Haiku tailoring, RenderCV) | ⬜ stub |
| 3 | Outreach drafting (contact lookup, copy, pending-review queue) | ⬜ stub |
| 4 | Full scheduling, dual-trigger, digest, alerts | ⬜ stub |

## Project layout

```
.
├── pyproject.toml               # uv/pip project + tool config (Python 3.12)
├── requirements*.txt            # pip fallback
├── .env.example                 # config placeholders (no secrets committed)
├── companies.yaml               # sourcing targets read by the pipeline (Phase 1)
├── companies.example.yaml       # documented template for companies.yaml
├── ACTIONS_FOR_PAUL.md          # manual (non-code) setup steps
├── .github/workflows/
│   ├── ci.yml                   # lint + test on push/PR
│   └── daily.yml                # daily sourcing cron (0 13 * * *)
├── src/internship_pipeline/
│   ├── config.py                # pydantic-settings
│   ├── logging_config.py        # structured JSON logging to stdout
│   ├── models.py                # Job, RunRecord, StageContext/Result
│   ├── run_daily.py             # orchestrator entrypoint
│   ├── sourcing/                # companies loader, http, ATS/Simplify/JSearch fetchers
│   ├── storage/                 # SQLite + Supabase backends + SQL migrations
│   ├── digest/                  # jinja2 HTML digest (render + write)
│   └── stages/                  # source → match_and_slice → draft_outreach
│       └── ...                  #        → prepare_applications → log_and_digest
└── tests/                       # unit tests + fixtures (no live APIs in tests)
```

## Phase 1: how sourcing works

`source()` reads `companies.yaml`, fetches each company's public ATS JSON feed,
plus the SimplifyJobs `listings.json` (and optionally JSearch), normalizes every
posting into one `Job`, dedupes by a stable hash of the URL, and upserts into the
`jobs` table. New rows (not previously stored) are the day's deltas. `log_and_digest()`
renders them to `data/digests/digest-YYYYMMDD.html` (+ `latest.html`). **No email is
sent in Phase 1** — the file *is* the digest (and the GitHub Actions run uploads it
as an artifact).

**ATS feed endpoints** (exactly the blueprint's — confirmed against live feeds):

| ATS | Feed |
| --- | ---- |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` |

Edit `companies.yaml` (schema documented in `companies.example.yaml`) to add real
targets — replace each `slug` with the company's real board token. Placeholder rows
are skipped, so it runs clean before you fill them in.

**Storage:** `STORAGE_BACKEND=supabase` (primary; needs `SUPABASE_URL`/`SUPABASE_KEY`,
run `src/internship_pipeline/storage/sql/postgres.sql` once) or `sqlite` (local
default, `data/pipeline.db`). If Supabase creds are missing it falls back to SQLite.

## Setup

Target runtime: **Python 3.12**. Dependency management: **uv** (pip fallback).

```bash
# Recommended (uv)
uv sync --extra dev

# Fallback (pip)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env            # Phase 0 needs no secrets
```

## Run

```bash
uv run python -m internship_pipeline.run_daily                 # all stages
uv run python -m internship_pipeline.run_daily --stage source  # one stage
uv run python -m internship_pipeline.stages.source             # stage, standalone
```

## Test / lint

```bash
uv run pytest
uv run ruff check .
```

## Scheduling (GitHub Actions)

`.github/workflows/daily.yml` runs the pipeline daily at `cron: '0 13 * * *'`
(13:00 UTC ≈ early US morning), reads Supabase/RapidAPI creds from repo secrets,
uploads the digest as an artifact, and has a stubbed `if: failure()` notify step
(GitHub does not alert on scheduled-workflow failures). See
[`ACTIONS_FOR_PAUL.md`](./ACTIONS_FOR_PAUL.md) to enable it.

> **⚠️ Keep-alive (60-day auto-disable):** GitHub **disables scheduled workflows
> after 60 days with no repository activity.** To keep the daily cron alive, push a
> trivial commit at least once every ~8 weeks — e.g. a **weekly** no-op commit or
> doc tweak. (Phase 4 adds an automated keep-alive job; until then, do it by hand.)

## Design principles (from the blueprint)

- **Deterministic Python everywhere; LLM only for reasoning steps** (matching,
  tailoring, drafting).
- **Each stage is idempotent and independently runnable.** External calls get
  retry-with-backoff (`tenacity`) and defensive, skip-on-error parsing.
- **Humans gate every outbound action** (sending email, submitting applications).
  The system *prepares*; you *send/submit*. Never automate LinkedIn.
- **No fabrication.** Résumé tailoring only reorders/rephrases real bullets you
  provide — it never invents experience, metrics, or skills.

See [`ACTIONS_FOR_PAUL.md`](./ACTIONS_FOR_PAUL.md) for the manual (non-code) setup steps.

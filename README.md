# Automated Internship Workflow

A scheduled, logged, **human-in-the-loop** internship-hunting pipeline:
deterministic Python orchestration with **narrow LLM calls** only where reasoning
helps (matching, résumé tailoring, outreach drafting). The full design and the
validated facts (API endpoints, costs, legal guardrails) live in
[`Automated Intern Recruitment Workflow.md`](./Automated%20Intern%20Recruitment%20Workflow.md)
— that blueprint is the single source of truth.

> **Status: Phase 2 (résumé slicing + application drafting) — complete.** On top of
> Phase 1 sourcing, each new job is scored against a tagged master résumé by
> embedding similarity, its JD keywords are extracted, a one-page résumé is tailored
> from **real bullets only** (Claude Haiku, with a hard anti-hallucination guardrail)
> and rendered to PDF via RenderCV, and standard application answers are drafted —
> all stored `pending_review`. Runs end-to-end with **zero credentials** (deterministic
> fallbacks). Stages 3–4 remain **stubs**, each built in its own phase.

## Phased roadmap

| Phase | Scope | State |
| ----- | ----- | ----- |
| 0 | Repo scaffold, tooling, config, logging, models, stage skeleton, tests | ✅ done |
| 1 | Sourcing + tracking (ATS feeds, SimplifyJobs JSON, dedupe, store, digest) | ✅ done |
| 2 | Résumé slicing + application drafting (embeddings, Haiku tailoring, RenderCV) | ✅ done |
| 3 | Outreach drafting (contact lookup, grounded copy, human-gated send) | ✅ done |
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

## Phase 2: how tailoring works

`match_and_slice()` takes the day's new jobs and, for each one, uses the tagged
`master_resume.yaml` (single source of truth) to:

1. **Score fit** — embed the JD and every résumé bullet (`sentence-transformers`
   locally by default; a deterministic hashing embedder is the offline fallback),
   take cosine similarity, and set `fit_score` = mean of the top-K bullet
   similarities. Below `FIT_SCORE_THRESHOLD`, the job is skipped.
2. **Extract JD keywords** — cheap, deterministic frequency + tech-vocab scoring
   (no LLM), used both to bias tailoring and to store on the application.
3. **Tailor a one-page résumé** — Claude Haiku (`ANTHROPIC_MODEL`, low temperature,
   master-résumé context sent with prompt caching) selects/reorders/rephrases the
   top-K **real** bullets. A hard Python guardrail then rejects any rephrase that
   introduces a token not present in the tailoring input, falling back to the
   verbatim bullet — so no fabricated metric, employer, or skill can reach the
   résumé. With no API key, this degrades to deterministic select-only.
4. **Render a PDF** — one `rendercv render` CLI call turns the tailored YAML into a
   PDF; the artifact path is stored on the application (YAML kept if RenderCV isn't
   installed).

`prepare_applications()` then drafts answers to standard application questions
(real-data-only) per job. Everything is written to the `applications` table as
`pending_review` — **nothing is ever auto-submitted**. High-fit or target-company
roles are flagged `human_review`.

Every heavy dependency (`sentence-transformers`, `anthropic`, `rendercv`) is
lazy-imported and optional — the pipeline always runs with zero credentials. Install
what you want with the extras: `uv sync --extra phase2` (or `--extra ml` / `--extra
llm` / `--extra render`).

## Phase 3: how outreach works

`draft_outreach()` runs after tailoring and, for each prepared application:

1. **Resolve a contact** — tries Hunter.io then Apollo.io **only** when a provider is
   enabled + keyed, only for high-priority roles (`OUTREACH_PAID_LOOKUP_HIGH_PRIORITY_ONLY`),
   and only within a hard per-run cap (`OUTREACH_MAX_LOOKUPS_PER_RUN`) — the free-tier
   guard. Otherwise (and by default) it falls back to a company email-pattern **guess**
   returned as `verified=False`, `confidence=None` with an explicit "this is a guess"
   note, so an unconfirmed address is never mistaken for a real one.
2. **Draft grounded copy** — a short email + a ≤300-char LinkedIn note, reusing the
   **same real top bullets** Phase 2 retrieved. Claude when configured, deterministic
   template otherwise; either way the same Python guardrail rejects any field that
   introduces a fact outside the job text + real profile, falling back to the grounded
   template. No fabricated project, metric, or employer can reach a message.
3. **Persist two `pending_review` rows** — one `email` (with the CAN-SPAM footer baked
   into the exact body that would send) and one `linkedin` (draft-only, no footer).
   Recipients on the do-not-contact list (DB + optional `OUTREACH_SUPPRESSION_FILE`)
   are flagged `suppressed`.

**Nothing is ever auto-sent.** Sending is a separate, manual command with its own gate:

```bash
python -m internship_pipeline.outreach.approve_and_send <outreach_id>        # PREVIEW only
python -m internship_pipeline.outreach.approve_and_send <outreach_id> --yes  # actually send
python -m internship_pipeline.outreach.suppress add someone@company.com      # never contact
```

The send gate refuses unless: the channel is `email` (LinkedIn is draft-only — you send
those yourself), a real recipient exists, the contact isn't suppressed, the CAN-SPAM
footer is present, and `OUTREACH_PHYSICAL_ADDRESS` is a real address (not the shipped
placeholder). Gmail is reached only after an explicit `--yes`. See `ACTIONS_FOR_PAUL.md`.

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

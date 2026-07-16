# Automated Internship Workflow

A scheduled, logged, **human-in-the-loop** internship-hunting pipeline:
deterministic Python orchestration with **narrow LLM calls** only where reasoning
helps (matching, résumé tailoring, outreach drafting). The full design and the
validated facts (API endpoints, costs, legal guardrails) live in
[`Automated Intern Recruitment Workflow.md`](./Automated%20Intern%20Recruitment%20Workflow.md)
— that blueprint is the single source of truth.

> **Status: Phase 5 — complete. All six stages are wired end-to-end.** The daily run
> sources roles → scores + tailors a résumé per role (one CV per cluster of similar
> roles) → drafts outreach for the roles worth it (dual-trigger) → drafts application
> answers (the job's REAL form questions where Greenhouse exposes them) → syncs the
> **Google Sheets tracker** (rows with durable Drive CV links) → emails you a slim,
> outreach-focused digest. Everything stays `pending_review`. Runs end-to-end with
> **zero credentials** (deterministic fallbacks + no-op integrations) — try `--dry-run` below.

## Phased roadmap

| Phase | Scope | State |
| ----- | ----- | ----- |
| 0 | Repo scaffold, tooling, config, logging, models, stage skeleton, tests | ✅ done |
| 1 | Sourcing + tracking (ATS feeds, SimplifyJobs JSON, dedupe, store, digest) | ✅ done |
| 2 | Résumé slicing + application drafting (embeddings, Haiku tailoring, LaTeX render) | ✅ done |
| 3 | Outreach drafting (contact lookup, grounded copy, human-gated send) | ✅ done |
| 4 | Orchestration: dual-trigger, digest email + reply scan, alerts, keep-alive, dry-run | ✅ done |
| 5 | Google Sheets tracker + Drive CV store, CV grouping/cache, real ATS questions, Gmail outreach drafts, slim digest | ✅ done |
| 6a | Networking campaigns (8VC seed): LinkedIn ladder state machine, drafted connect notes/messages, Networking sheet tab, digest actions — LinkedIn always human-sent | ✅ done |
| 6b | Networking email escalation: contact lookup, Gmail drafts, reply detection, follow-up | planned |

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
│   ├── config.py                # pydantic-settings (+ build_dry_run_settings)
│   ├── logging_config.py        # structured JSON logging to stdout
│   ├── models.py                # Job, Application, Outreach, RunRecord, StageContext/Result
│   ├── run_daily.py             # orchestrator entrypoint (--dry-run, --stage)
│   ├── triggers.py              # Phase 4 dual-trigger (favorable + high-fit)
│   ├── alerts.py                # Phase 4 failure alert (email impl, slack stub)
│   ├── sourcing/                # companies loader, http, ATS/Simplify/JSearch fetchers,
│   │                            #   real Greenhouse form questions (Phase 5)
│   ├── resume/                  # embeddings, matching, tailoring, LaTeX render (Resume.tex
│   │                            #   style), answers, CV grouping + cache keys (Phase 5)
│   ├── review/                  # local CV review app (pick bullets, preview, submit)
│   ├── outreach/                # contacts, copy, footer, suppress, gmail, replies, send,
│   │                            #   Gmail outreach drafts (Phase 5)
│   ├── tracker/                 # Phase 5: Google Sheets tracker + Drive CV store + backfill
│   ├── storage/                 # SQLite + Supabase backends + SQL migrations
│   ├── digest/                  # jinja2 HTML digest (render + write + email)
│   ├── fixtures/                # bundled dry-run jobs + résumé (no creds needed)
│   └── stages/                  # source → match_and_slice → draft_outreach
│       └── ...                  #   → prepare_applications → sync_tracker → log_and_digest
└── tests/                       # unit tests + fixtures (no live APIs in tests)
```

## Phase 1: how sourcing works

`source()` reads `companies.yaml`, fetches each company's public ATS JSON feed,
plus the SimplifyJobs `listings.json` (and optionally JSearch), normalizes every
posting into one `Job`, dedupes by a stable hash of the URL, and upserts into the
`jobs` table. New rows (not previously stored) are the day's deltas. `log_and_digest()`
renders them to `data/digests/digest-YYYYMMDD.html` (+ `latest.html`). **No email is
sent in Phase 1** — the file *is* the digest.

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
4. **Render a PDF** — the tailored bullets are typeset with the user's own
   `Resume.tex` LaTeX template (`resume/latex.py`) and compiled with whatever
   engine is on PATH (tectonic recommended; xelatex/pdflatex also work) into a
   per-job PDF (`data/resumes/<job-hash>.pdf`). The render **trims
   least-relevant-last until the page count is exactly one** — the tailoring is
   deliberately generous (`MAX_TAILORED_BULLETS`, default 16) so the page ends up
   full, not sparse. With no engine installed the YAML/.tex artifacts are still
   written. **Links are first-class:** a project's `url:` renders its name as the
   template's blue `\repolink`, Markdown `[text](url)` inside bullet text renders
   clickable too, and the grounding guardrail rejects any LLM rephrase that drops
   or alters a link (verbatim fallback).

A cost guard caps LLM/render volume: every new job is scored locally, but only the
top `MAX_APPLICATIONS_PER_RUN` (default 15) by fit get tailoring + a PDF per run.

`prepare_applications()` then drafts answers to a job's **real** application-form
questions when Greenhouse exposes them (real-data-only). Jobs with no visible
free-text questions are skipped — no generic fallback set, no wasted LLM call.
Everything is written to the `applications` table as `pending_review` — **nothing
is ever auto-submitted**. High-fit or target-company roles are flagged `human_review`.

**Human CV review (`make review`)** — `python -m internship_pipeline.review` opens a
local web app over the pending applications: the AI's recommended experience/project
bullets come prechecked (header, education, skills are automatic), you toggle any
bullet, preview the compiled PDF with a live page count, and hit *Submit* — that
finalizes the CV, uploads it to Drive (when configured), marks the application
`reviewed`, and pushes its row to the tracker sheet. **Unreviewed applications never
reach the sheet**; stale ones simply expire out of the queue.

Every heavy dependency (`sentence-transformers`, `anthropic`, `pypdf`) is
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

## Phase 4: how orchestration ties together

`run_daily.py` runs the five stages in order, each wrapped so a failure is logged and
**skipped** (one bad stage never kills the run); counts + errors aggregate into a `runs`
row and the run's `success`/`partial`/`failed` status. Stages are idempotent (dedupe by
URL hash → re-runs are safe) and share state through `ctx.data`, not return values.

```
source → match_and_slice → draft_outreach → prepare_applications → log_and_digest
```

- **Dual-trigger** (`triggers.py`). A role earns *both* a prepared application **and** a
  drafted outreach message only when it's **high-fit** (`fit_score >= HIGH_PRIORITY_THRESHOLD`)
  **and favorable** (a `TARGET_COMPANIES` name **or** posted within `FAVORABLE_RECENT_DAYS`).
  Everything above `FIT_SCORE_THRESHOLD` still gets an application; only dual-trigger roles
  also get outreach — keeping scarce/paid contact lookups aimed where they're worth it.
  (The recency test is an honest stand-in for "deadline soon": the free feeds don't expose
  a hard application deadline, so a freshly-posted role is the "act now" signal.)
- **Morning digest** (`log_and_digest`). One HTML file (`data/digests/latest.html`) —
  since Phase 5 it is deliberately SLIM: a count header (new jobs, applications prepared,
  LLM calls saved) + one link to the Google Sheet tracker, the outreach drafts (with
  Gmail-draft links), and a best-effort Gmail **reply scan**. With `DIGEST_EMAIL_ENABLED`
  + Gmail configured it's **emailed to yourself** — the one outbound action the daily run
  performs (sending outreach / submitting applications is always you). Replies from
  contacts you actually emailed transition those outreach rows `sent → replied`.
- **Observability + alerts.** Structured JSON logs, a persisted `runs` row, retry-with-backoff
  on every external call (`tenacity`), and a real `if: failure()` step that emails you via
  Gmail (`alerts.py`; Slack is a documented stub — set `ALERT_CHANNEL=slack` to switch).
- **Keep-alive.** `.github/workflows/keepalive.yml` makes a weekly no-op commit so the
  scheduled cron isn't auto-disabled after 60 days of repo inactivity.
- **LangGraph: intentionally not used.** The human-in-the-loop branch is already a
  `pending_review` **status column** + a separate manual `approve_and_send` command — the
  checkpoint *is* the DB row plus your CLI action. Wrapping that in LangGraph `interrupt()`
  would add a heavyweight dependency and a second orchestration model for no behavioral gain,
  so per the assignment's "don't over-engineer" guidance it was skipped.

## Phase 5: the Google Sheet is the application workspace; the inbox is for outreach

The two human touchpoints are split by what they're *for*:

- **Google Sheet = the application workspace** (`sync_tracker` stage, `tracker/`).
  Every prepared application is a row on the **Applications** tab: clickable job link,
  prepared date, company, locations, a **Status dropdown**
  (`prepared → submitted → interviewing → offer → rejected → withdrawn` — the pipeline
  only ever writes `prepared`; every transition after that is yours), a **Notes** column
  the pipeline never touches, the **CV** link (durable, in Google Drive), a link to the
  drafted **Answers** tab, fit score, and JD keywords. Upsert is idempotent by a hidden
  dedupe-key column, and after the initial insert the pipeline only fills cells that are
  still blank — your edits always win. Setting a row's Status to **`rejected`** or
  **`withdrawn`** are the actions the pipeline acts on: the next sync (daily, or any
  review submit) deletes that row from the sheet and marks the stored application with
  that same status so it never comes back — storage keeps the history, and the daily
  digest reports how many rows were removed. Storage (Supabase/SQLite) stays the source of
  truth; the sheet is a projection.
- **Durable CVs in Drive** (`tracker/drive.py`). Tailored PDFs used to die with the
  ephemeral CI runner (artifacts can't be published — public repo, PII). Now each
  rendered PDF is uploaded to a shared Drive folder and the `webViewLink` is stored on
  the application (`cv_drive_link`) and shown in the sheet. Recover everything older
  runs already prepared with the one-off backfill (re-renders from stored YAML — free,
  no LLM): `python -m internship_pipeline.tracker.backfill`.
- **CV grouping + cache — the LLM cost saver** (`resume/grouping.py`). Before
  tailoring, the capped job list is clustered on the JD embeddings already computed for
  scoring (cosine ≥ `CV_GROUP_SIMILARITY`, keyword-overlap sanity check). One cluster =
  one tailoring call + one render + one upload; other members' CV cells read
  `same as row N`. A persistent `cv_cache` table (keyed by selected-bullet ids +
  keyword set) reuses CVs across runs too. The digest header reports LLM calls saved.
- **Real ATS form questions** (`sourcing/questions.py`). Where Greenhouse exposes a
  job's actual application form (`.../jobs/{id}?questions=true` — response shape
  verified against a live board), `prepare_applications` drafts answers to those
  free-text questions only (selects like work authorization are never drafted —
  they're yours). Lever/Ashby's public APIs expose no form fields (checked), so
  those jobs are skipped — no generic fallback questions. `MAX_QUESTION_DRAFTS_PER_RUN`
  caps LLM drafting calls (fetches are free); best-fit jobs with visible questions
  are drafted first. Drafted answers land on the **Answers** tab (question, drafted
  answer, and an *edited-answer column that's yours*).
- **Outreach lands as real Gmail drafts** (`outreach/drafts.py`, flag-gated by
  `OUTREACH_GMAIL_DRAFTS_ENABLED`). Eligible drafts (verified contact, not suppressed)
  are created via the Gmail API `drafts.create` — you open Gmail, edit, and hit send.
  This is *drafting*, not sending: the human gate is intact, `approve_and_send` still
  works, and rows transition `pending_review → gmail_draft_created → sent → replied`.
  Requires the `gmail.compose` scope (re-mint the token — see `ACTIONS_FOR_PAUL.md`).

Zero-credential rule still holds: with no service account / spreadsheet configured the
tracker stage logs one line and no-ops, Drive upload is skipped, and the digest keeps
working from the local file.

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

## Run locally

```bash
# End-to-end DRY RUN — every stage, bundled fixtures, ZERO credentials. Start here.
uv run python -m internship_pipeline.run_daily --dry-run

uv run python -m internship_pipeline.run_daily                 # all stages (live)
uv run python -m internship_pipeline.run_daily --stage source  # one stage (repeatable)
uv run python -m internship_pipeline.stages.match_and_slice    # a stage, standalone

# Manual, human-gated actions (never run by the daily pipeline):
uv run python -m internship_pipeline.outreach.approve_and_send <outreach_id>        # preview
uv run python -m internship_pipeline.outreach.approve_and_send <outreach_id> --yes  # send
uv run python -m internship_pipeline.outreach.suppress add someone@company.com      # block
uv run python -m internship_pipeline.outreach.gmail_auth                            # one-time OAuth

# One-off recovery (Phase 5): re-render lost CV PDFs from stored YAML → Drive + sheet
uv run python -m internship_pipeline.tracker.backfill --dry-run                     # report
uv run python -m internship_pipeline.tracker.backfill                               # do it
```

The daily run always writes `data/digests/latest.html` (open it). With the SQLite backend
(default) the tracker is `data/pipeline.db`.

## Test / lint

```bash
uv run pytest
uv run ruff check .
```

## Configuration & secrets

Config is `pydantic-settings`: every field maps to an UPPER_CASE env var (`.env` locally,
repo **variables**/**secrets** in Actions). **Everything is optional** — with none set the
pipeline runs fully offline with deterministic fallbacks. Enable a capability by setting its
vars. Full manual walkthrough: [`ACTIONS_FOR_PAUL.md`](./ACTIONS_FOR_PAUL.md).

| Secret / var | Phase | Enables (unset ⇒ fallback) |
| --- | --- | --- |
| `SUPABASE_URL`, `SUPABASE_KEY` | 1 | Supabase tracker (`STORAGE_BACKEND=supabase`); else local SQLite |
| `RAPIDAPI_KEY` (+ `ENABLE_JSEARCH`) | 1 | JSearch tertiary source; else Simplify + ATS only |
| `ANTHROPIC_API_KEY` | 2 | Claude résumé tailoring + answer/outreach drafting; else deterministic |
| `TARGET_COMPANIES`, `FIT_SCORE_THRESHOLD`, `HIGH_PRIORITY_THRESHOLD` | 2/4 | matching + dual-trigger thresholds |
| `FAVORABLE_RECENT_DAYS` | 4 | "posted recently" favorability window (dual-trigger) |
| `ENABLE_HUNTER`+`HUNTER_API_KEY`, `ENABLE_APOLLO`+`APOLLO_API_KEY` | 3 | verified contact lookup; else free pattern-guess |
| `OUTREACH_FROM_NAME`, `OUTREACH_FROM_EMAIL`, `OUTREACH_PHYSICAL_ADDRESS` | 3 | CAN-SPAM identity — **required to send email** |
| `GMAIL_TOKEN_JSON` (secret) → `GMAIL_OAUTH_TOKEN_JSON` (path) | 3/4 | Gmail send (approve-and-send), digest email, reply scan, failure alert |
| `MASTER_RESUME_YAML` (secret) | 2 | full contents of `master_resume.yaml` (PII, git-ignored) — **required in Actions** or the scheduled run skips matching/tailoring/outreach entirely |
| `DIGEST_EMAIL_ENABLED`, `DIGEST_TO_EMAIL` | 4 | email the morning digest to yourself |
| `ALERT_CHANNEL` (`email`\|`slack`), `SLACK_WEBHOOK_URL` | 4 | failure alert channel (email implemented; slack stub) |
| `REPLY_SCAN_DAYS`, `REPLY_SCAN_QUERY` | 4 | recruiter-reply Gmail scan window/terms |
| `TRACKER_SHEETS_ENABLED`, `GOOGLE_SERVICE_ACCOUNT_JSON` (secret), `SHEETS_SPREADSHEET_ID`, `DRIVE_FOLDER_ID` | 5 | Google Sheets tracker + Drive CV store; unset ⇒ sync stage no-ops |
| `CV_GROUP_SIMILARITY`, `CV_GROUP_KEYWORD_OVERLAP` | 5 | CV grouping thresholds (one tailored CV per cluster of similar JDs) |
| `MAX_QUESTION_DRAFTS_PER_RUN` | 5 | cap on answer-drafting LLM calls per run (only jobs with visible Greenhouse questions; fetches are free) |
| `OUTREACH_GMAIL_DRAFTS_ENABLED` | 5 | land outreach as real Gmail drafts (needs the `gmail.compose` scope — re-mint the token) |

## Scheduling (GitHub Actions)

- **`daily.yml`** — the production cron, `0 13 * * *` (13:00 UTC ≈ early US morning). Reads
  the vars/secrets above, runs the pipeline, syncs the tracker sheet, emails the digest
  to you (if enabled), and on failure emails a real alert (`if: failure()` → `alerts.py`,
  since GitHub doesn't notify on scheduled-workflow failures). Manually triggerable via
  **workflow_dispatch**. No digest artifact is uploaded — the repo is public and the
  digest carries PII; Drive + the sheet are the durable outputs.
- **`keepalive.yml`** — weekly no-op commit so the schedule isn't auto-disabled (below).
- **`ci.yml`** — lint + tests + a `--dry-run` smoke on every push/PR.

> **⚠️ 60-day auto-disable:** GitHub disables scheduled workflows after 60 days of repo
> inactivity. `keepalive.yml` handles this automatically with a weekly commit; if you ever
> disable it, push a trivial commit at least every ~8 weeks by hand.

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

# ACTIONS FOR PAUL

---

## Phase 1 

### 2. (Optional) Add specific companies via direct ATS feeds

`companies.yaml` is **not required** — `ENABLE_SIMPLIFY` is on by default and
already pulls ~1,200+ live internship postings across hundreds of companies from
the community-maintained SimplifyJobs list, no setup needed. Only touch this if
there's a specific company you want to track that Simplify might miss or lag on
(direct-from-source is fresher and catches companies outside Simplify's list).

Edit `companies.yaml` (schema documented in `companies.example.yaml`). For each
target, find its real board token on its careers page and set `ats` + `slug`:


| ATS        | Where the slug comes from                                                |
| ---------- | ------------------------------------------------------------------------ |
| greenhouse | `boards.greenhouse.io/<slug>` (or the company's GH-powered careers page) |
| lever      | `jobs.lever.co/<slug>`                                                   |
| ashby      | `jobs.ashbyhq.com/<slug>`                                                |


Placeholder (`REPLACE_ME_*`) rows are skipped, so this is safe to leave as-is.
Commit `companies.yaml` if you add real entries — the daily GitHub Actions run
reads it from the checkout.

Run it locally to confirm: `uv run python -m internship_pipeline.run_daily`, then
open `data/digests/latest.html`.

### 3. (Optional) JSearch / RapidAPI — tertiary source

Only if you want aggregator breadth. Free BASIC plan = **200 requests/month**,
hard-capped.

1. Subscribe to JSearch (BASIC/free) at
  [https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch).
2. Copy your RapidAPI key → secret name `RAPIDAPI_KEY`.
3. Enable with repo variable `ENABLE_JSEARCH=true` (off by default; the run
  skips JSearch cleanly when the key is absent).



### 4. Enable the daily GitHub Actions cron

In your GitHub repo → **Settings → Secrets and variables → Actions**:

- **Variables** tab → add:
`STORAGE_BACKEND` = `supabase` (or `sqlite`), and (optional) `ENABLE_JSEARCH` = `true`.

Then enable workflows: **Actions** tab → enable if prompted. The workflow
`.github/workflows/daily.yml` runs at `0 13 * * *` UTC; you can also trigger it
manually via **Run workflow** (workflow_dispatch). The digest is uploaded as a
build **artifact** on each run.

> **Keep-alive:** GitHub disables scheduled workflows after **60 days** of repo
> inactivity. Push a trivial commit ~weekly to keep the cron alive (see README).



### 5. Confirm the `# VERIFY` items

I confirmed the **Greenhouse / Lever / Ashby / SimplifyJobs** response fields
against the live feeds, so those are solid. The one source I could **not** verify
(it needs a key) is **JSearch** — field names in
`src/internship_pipeline/sourcing/jsearch.py` are marked `# VERIFY`. If you enable
JSearch and the results look wrong/empty, paste me one raw JSearch `data[]` item
and I'll correct the mapping. (Parsing is defensive, so a wrong field name just
skips the row rather than crashing.)

---



## Future phases — do NOT do yet (preview only)

- **Phase 2:** Anthropic Console API key → `ANTHROPIC_API_KEY` (Claude Haiku 4.5).
- **Phase 3:** Hunter.io → `HUNTER_API_KEY`; Apollo.io → `APOLLO_API_KEY`;
Gmail OAuth → `GMAIL_OAUTH_TOKEN_JSON`.
- **Phase 4:** Slack incoming webhook → `SLACK_WEBHOOK_URL` (wire the real failure
alert + an automated keep-alive job; turn the digest file into a sent email).


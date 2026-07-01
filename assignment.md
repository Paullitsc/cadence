Build PHASE 1 only: sourcing + tracking. Do not build later phases.

Goal: every run produces a fresh, deduped list of new internships written to a database.
Zero LLM cost in this phase.

Implement:
1. Project scaffold: `pyproject.toml` (uv), `.env.example`, `README.md`, `src/` package,
   `tests/`, and a single entrypoint `run_daily.py` that calls modular stage functions.
   For Phase 1 only `source()` and `log_and_digest()` need real bodies; stub the rest.
2. `companies.yaml`: a small schema + 3-5 example entries (placeholder slugs) mapping each
   target company to its ATS provider and board token/slug. Document the format; I'll fill
   in real companies.
3. ATS feed fetchers using EXACTLY these endpoints from the blueprint (no others invented):
   - Greenhouse: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
   - Lever:      https://api.lever.co/v0/postings/{company}?mode=json
   - Ashby:      https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
   Normalize all three into one `Job` pydantic model. If you're unsure of a response field
   name, mark `# VERIFY` and ask me rather than guessing.
4. SimplifyJobs puller: fetch the raw `listings.json` from SimplifyJobs/Summer2026-Internships
   with fields company_name, title, locations, url, date_posted, active, source; diff against
   what's stored and surface new active roles. (Confirm the raw path with me before hardcoding
   if you're not certain.)
5. JSearch (RapidAPI) fetcher as an OPTIONAL tertiary source, behind a feature flag and the
   free 200-req/month cap in mind. Skip cleanly if no API key is set.
6. Storage: Supabase (Postgres) as primary with a SQLite fallback for local dev. Tables:
   `jobs` (dedupe by URL/hash), `runs`. Provide the SQL/migrations. Dedupe by stable job hash.
7. `log_and_digest()`: compute "new jobs today" and render an HTML digest (jinja2). For
   Phase 1, WRITE the digest to a local file / log it; do NOT wire up real email sending yet.
8. GitHub Actions workflow `.github/workflows/daily.yml`: `cron: '0 13 * * *'`, pins Python,
   installs deps, runs `run_daily.py`, reads secrets from repo secrets, plus an
   `if: failure()` notify step (stubbed) and a weekly keep-alive note in the README about the
   60-day auto-disable.
9. Tests with fixture JSON for each fetcher + dedupe.

End with the "ACTIONS FOR PAUL" checklist (Supabase project + URL/keys, optional RapidAPI
key, which GitHub repo secrets to set and their exact names, filling real companies into
companies.yaml, enabling Actions).
# Blueprint: A Cheap, Scheduled, Logged Agentic Internship-Hunting Pipeline
- ## TL;DR
  collapsed:: true
	- **Build a hybrid system: your own Python codebase (the "brain") orchestrated by a daily GitHub Actions cron, calling the Anthropic API with Claude Haiku 4.5 ($1/$5 per million tokens) for all LLM work, and writing to a free Supabase Postgres or Google Sheet.** Sourcing, resume-slicing, outreach drafting, and tracking can be fully and safely automated for roughly $2–10/month; application *submission* and outreach *sending* should be human-in-the-loop, not fully autonomous.
	- **Sourcing is the cheapest, most reliable stage** — pull directly from free public ATS JSON feeds (Greenhouse, Lever, Ashby) and the SimplifyJobs/Pitt CSC `listings.json` on GitHub, supplemented by JSearch (free tier) — and the resume-slicing engine is where LLMs add the most value (tag bullets in YAML, retrieve by embedding similarity to the JD, tailor with a prompt, render a PDF with RenderCV).
	- **Auto-*submitting* applications and auto-*sending* cold messages are the danger zones**: Workday/Greenhouse fraud-detection, CAPTCHAs, LinkedIn automation bans, and CAN-SPAM/GDPR all argue for a "prepare draft → human approves → send/submit" gate. Reserve the dual cold-outreach + full-application trigger for your highest-priority targets, where quality matters most.
- ## Key Findings
  collapsed:: true
	- 1. **Architecture: hybrid wins.** A pure agent-framework approach (CrewAI/AutoGen/LangGraph fully autonomous) is overkill and burns tokens; pure no-code (n8n/Make) limits the custom resume logic. The sweet spot for a technical user is **deterministic Python orchestration with narrow LLM calls** at the steps that need reasoning (matching, tailoring, drafting). LangGraph is worth adding only if you want stateful human-in-the-loop checkpoints.
	- 2. **Scheduling: GitHub Actions cron is free and sufficient.** Public repos get unlimited Actions minutes; private repos get 2,000 free minutes/month. A daily morning run of a few minutes costs nothing. Caveats: minimum 5-minute interval, runs in UTC, can be delayed 10–30+ minutes at peak, and scheduled workflows auto-disable after 60 days of repo inactivity.
	- 3. **LLM cost: use the Anthropic API pay-as-you-go with Haiku, not your Claude Pro sub.** Claude.ai consumer plans (Pro at $20/month) do not include API access — the API is separate pay-as-you-go billing (PE Collective, 2026). Per Anthropic's Oct 15, 2025 launch page, "Pricing for Haiku 4.5 on the Claude Platform starts at $1 per million input tokens and $5 per million output tokens, with up to 90% cost savings with prompt caching and 50% cost savings with batch processing." A daily internship run is well under $0.50/day. Claude Code/Agent SDK on a subscription is an alternative but its credit model is in flux.
	- 4. **Sourcing has excellent free options.** Greenhouse (`boards-api.greenhouse.io/v1/boards/{token}/jobs`), Lever (`api.lever.co/v0/postings/{company}`), and Ashby (`api.ashbyhq.com/posting-api/job-board/{name}`) expose **public, no-auth JSON feeds**. SimplifyJobs/Summer2026-Internships stores everything in a structured `listings.json`. JSearch (RapidAPI) offers 200 free requests/month.
	- 5. **Email finding & sending have usable free tiers but watch the law.** Hunter.io free = 25–50 searches/month; Apollo.io free = ~100 credits/month (some sources cite higher). Resend free = 3,000 emails/month (100/day cap) permanently. Gmail API caps a free personal account at ~500 sends/day (the Gmail API itself imposes a 500-recipients-per-message limit and bills nothing for standard use; Workspace accounts go to ~2,000 sends/day). CAN-SPAM allows cold email with honest headers + physical address + opt-out.
	- 6. **LinkedIn automation = real ban risk.** LinkedIn ToS prohibits automation; cloud tools (Dripify, Expandi, HeyReach, Waalaxy) carry the highest risk. Treat LinkedIn as manual-only or assist-only.
	- 7. **Auto-submitting applications is flaky and reputationally risky.** Workday hides fields in shadow DOM; Greenhouse runs invisible fraud scoring; Indeed throws CAPTCHAs. Browser-AI agents (Skyvern, browser-use) perform worst exactly on "write" tasks (forms/logins). Use autofill assist (Simplify Copilot, free) + human submit.
- ## Details
  collapsed:: true
	- ### 1. Overall Architecture
	  **Why hybrid:** Most pipeline steps (fetch ATS feeds, dedupe, write rows, render PDF, send digest) are deterministic and should be plain Python — there is no reason to pay an LLM to decide to call an HTTP endpoint. Reserve LLM calls for: (a) scoring/ranking a job against your profile, (b) selecting and tailoring resume bullets, (c) drafting outreach copy and cover-letter snippets. This keeps token cost near-zero and makes the system debuggable.
	- **Concrete stack:**
		- **Language/runtime:** Python 3.12, `uv` or `pip` for deps, `httpx` for APIs, `pydantic` for typed models, `jinja2` for templating.
	- **Orchestration:** a single `run_daily.py` entrypoint that calls modular stage functions (`source()`, `match_and_slice()`, `draft_outreach()`, `prepare_applications()`, `log_and_digest()`). Each stage is independently runnable and idempotent.
	- **State:** Supabase (free Postgres) or SQLite-in-repo for local; a `jobs` table (dedupe by URL/hash), `outreach` table, `applications` table, `runs` table (for the daily log).
	- **LangGraph layer:** if you want the "favorable job → both outreach + application, pending human approval" branch to be an explicit stateful graph with `interrupt()` checkpoints, LangGraph 1.0 (stable Oct 2025) is the cleanest tool; otherwise a status column (`pending_review`) in Postgres achieves the same with less code.
	- **Scheduling options compared:**
		- **GitHub Actions scheduled workflow (recommended):** free (unlimited minutes on public repos; 2,000 min/month private), secrets management built in, logs retained, runs in the cloud so your laptop can be off. Caveats: UTC only, ≥5-min granularity, peak delays of 10–30+ min, and **scheduled workflows auto-disable after 60 days of no repo activity** (mitigate with a trivial weekly commit or a keep-alive job). Add a "notify on failure" step (Slack webhook or email) because GitHub does **not** alert on scheduled-workflow failure.
	- ### 2. Stage-by-Stage Tooling
		- #### (a) Sourcing
			- **Primary (free, reliable, no scraping risk): public ATS JSON feeds.** Companies on Greenhouse, Lever, and Ashby expose unauthenticated JSON that powers their own careers widgets:
				- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
				- Lever: `https://api.lever.co/v0/postings/{company}?mode=json` (supports filtering by team, location, commitment)
				- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`
				- Also no-auth: Workable, Recruitee (JSON), Personio (XML feed). Maintain a `companies.yaml` of target companies and their ATS + slug; loop daily and pull. This is the backbone.
			- **Secondary: curated GitHub internship lists.** `SimplifyJobs/Summer2026-Internships` (and the Vansh/Ouckah and SpeedyApply mirrors) store all listings in `.github/scripts/listings.json` with fields `company_name, title, locations, url, date_posted, active, source`. Pull the raw JSON daily, diff against yesterday, surface new active roles. Free.
			  
			  **Tertiary: aggregator APIs for breadth.**
				- **JSearch (RapidAPI / OpenWeb Ninja):** real-time Google-for-Jobs data across LinkedIn/Indeed/Glassdoor; **free BASIC plan = 200 requests/month, hard-capped, no credit card.**
				- **SerpAPI Google Jobs:** 250 free searches/month; paid starts $75/mo for 5,000 — too pricey, stay on free tier or use cheaper alternatives (Serper 2,500 free).
		- #### (b) Cold Outreach
			- **Hunter.io:** free tier 25–50 searches/month; domain search returns email pattern + confidence. Has an API and MCP server.
			- **Apollo.io:** free tier ~100 credits/month per some sources (others cite higher on the free plan); 275M+ contacts, built-in sequences.
			- Free Hunter + Apollo credits plus guessing the company email pattern ([first.last@company.com](mailto:first.last@company.com)) covers low volume.
			-
			- ## Sending email:
				- **Gmail API / SMTP (recommended, free):** a free personal @gmail.com is capped at ~500 sends/day (well above what you should send), and the Gmail API charges nothing for standard use. Use OAuth + the Gmail API; you get authentic deliverability from your real address. 
				  
				  **LinkedIn automation —** Use the system to *draft* personalized LinkedIn messages into your tracker, then send them manually.
		- #### (c) Applications
			- **Reality check:** fully auto-submitting applications is the least reliable and most reputationally risky stage.
			- **ATS-specific friction:** Workday hides form fields in shadow DOM (hard for selectors); Greenhouse runs invisible fraud/risk scoring ("Real Talent" suite) including IP/VPN/email-age checks; Indeed gates with CAPTCHAs that defeat most extension tools.
			- **AI browser agents** (Skyvern, browser-use, Playwright/Puppeteer/Selenium): capable but agents "performed surprisingly poorly on write-heavy tasks (e.g., logging in, filling out forms, downloading files)" — exactly application work. They also cost LLM tokens per step. Both are open-source and BYO-LLM: **browser-use** is MIT-licensed (79k+ GitHub stars, ~89.1% on the read-heavy WebVoyager benchmark), and **Skyvern's** core is AGPL-3.0 (20,000+ stars, self-hostable via Docker, BYO-LLM including Ollama). Self-hosting is free; you pay only your own LLM token spend.
			- **Autofill tools:** Simplify Copilot (free, no application limit) autofills 80%+ of forms across Workday/Greenhouse/Lever/iCIMS and keeps you the one who clicks submit; LazyApply ($99+/yr) does high-volume auto-submit but reviewers report fabricated answers and low callback rates (~1–6% for fully automated vs higher for human-reviewed).
			- **Quality data point:** per Huntr's Q1 2026 Job Search Trends Report, "The 11-to-20 application bucket converts at 9.25%... 100 or more: 2.58%, less than a third of the rate seen at the low-volume end." Volume hurts.
			  
			  **Recommendation:** Your Python system *prepares* each application (tailored resume PDF + pre-written answers to common questions, stored in the tracker), then you use Simplify Copilot (free) to autofill and **you click submit**.
		- #### (d) Tracking / Logging
			- **Supabase (recommended):** free tier = 500 MB Postgres, real SQL, 2 active projects, commercial use allowed. Best for a technical user who wants real queries and joins.
			  
			  **Daily digest:** at end of `run_daily.py`, query the run's deltas (new jobs found, outreach drafted, applications prepared, responses detected via Gmail API) and email yourself an HTML summary via Gmail API. Log structured JSON (Python `logging` + a `runs` table row) for observability.
			- ### 3. The Personalization / Resume-Slicing Engine
			  
			  **Step 1 — Master resume as structured, tagged data.** Store your multi-page master resume in YAML where every bullet is an object with tags:
			  
			  ```yaml
			  experiences:
			  - company: Acme
			    role: SWE Intern
			    bullets:
			      - text: "Built a Kafka pipeline processing 2M events/day..."
			        tags: [backend, distributed-systems, java, data]
			        metrics: true
			  projects: [...]
			  skills: {languages: [...], frameworks: [...]}
			  ```
			  
			  This is the single source of truth. RenderCV uses exactly this YAML→PDF model and even ships an AI-agent "skill" with Pydantic validation.
			  
			  **Step 2 — Match & rank jobs (embeddings).** Embed each job description and each resume bullet (e.g., a cheap embedding model), compute cosine similarity, and score job-to-profile fit.  Use the similarity score to (a) prioritize which jobs to act on and (b) pre-select the most relevant bullets per job.
			  
			  **Step 3 — ATS keyword extraction.** Parse the JD for hard requirements/keywords (LLM or simple noun-phrase extraction). Ensure the tailored resume surfaces the candidate's genuine matching keywords (never fabricate) — this is what gets past ATS keyword filters.
			  
			  **Step 4 — Prompt-based tailoring.** Feed Haiku the JD, extracted keywords, and the top-K retrieved bullets; instruct it to select and lightly reorder/rephrase **only from real bullets** (anti-hallucination guardrail) to produce a one-page tailored YAML. Pin temperature low. Keep a human-review flag for high-priority roles.
			  
			  **Step 5 — Render PDF programmatically.** Best options for a technical user:
				- **RenderCV (recommended):** `pip install`, YAML→Typst→PDF, perfect typography, version-controllable, open-source (17k+ stars). Your tailoring step emits RenderCV YAML; one CLI call produces the PDF.
				  
				  **Step 6 — Outreach copy.** Same retrieval context feeds a prompt that drafts a short, specific cold email / LinkedIn note referencing the company and the 1–2 most relevant projects. Always human-reviewed before send.
				  
				  **Dual-trigger logic:** if `fit_score >= high_threshold` AND role flagged favorable (target company, deadline soon), enqueue BOTH a prepared application and a drafted outreach message, both in `pending_review` status.
	- ### 4. Scheduling & Observability
		- **Daily run:** GitHub Actions `on: schedule: - cron: '0 13 * * *'` (13:00 UTC ≈ morning US). Pin Python, install deps, run `run_daily.py`, store secrets (API keys, Gmail OAuth token, Supabase URL) in repo secrets.
		- **Logging:** structured `logging` to stdout (captured in Actions logs) + a `runs` table row capturing counts and errors. Optionally upload a JSON artifact per run.
		- **Failure handling/retries:** wrap each external call in retry-with-backoff (`tenacity`); make stages idempotent (dedupe by job hash) so a re-run is safe. Add an `if: failure()` step that emails you — GitHub does not notify on scheduled failures by default.
		- **Daily digest email:** new jobs found, top-N by fit score, outreach drafts awaiting approval, applications prepared awaiting submit, and any recruiter replies detected by scanning your Gmail for responses. This is your single morning touchpoint.
			- **Keep-alive:** a weekly no-op commit prevents the 60-day auto-disable.
	- ### 5. Phased Implementation Roadmap
	  
	  **Phase 1 — Sourcing + Tracking.** Build `companies.yaml`, ATS feed fetchers (Greenhouse/Lever/Ashby), SimplifyJobs JSON puller, dedupe, and write to Supabase/Sheets. Add GitHub Actions daily cron + a basic "new jobs today" email digest. **Outcome: every morning you get a fresh, deduped list of relevant new internships. Zero LLM cost.**
	  
	  **Phase 2 — Resume Slicing + Application Drafting.** Build the tagged master-resume YAML, embedding-based job↔bullet matching and fit scoring, JD keyword extraction, Haiku tailoring prompt with anti-hallucination guardrail, and RenderCV PDF rendering. Store tailored resume + drafted common answers per job in the tracker. **Outcome: for each high-fit job, a tailored resume PDF + answers are ready; you autofill with Simplify Copilot and submit.**
	  
	  **Phase 3 — Outreach Drafting.** Add Hunter/Apollo contact lookup, the outreach-copy prompt, and the `pending_review` outreach queue. Wire Gmail API send behind a manual approval (a "send" command or a button), with suppression-list and CAN-SPAM footer. **Outcome: personalized recruiter notes drafted automatically, sent by you after a glance.**
	  
	  **Phase 4 — Full Scheduling, Dual-Trigger & Digest .** Implement the favorable-job dual trigger (both application + outreach prepared), failure alerts, retries, keep-alive, and the polished morning digest including detected recruiter replies. Optionally wrap the human-in-the-loop branch in LangGraph. **Outcome: a hands-off-until-approval daily pipeline within budget.**
- ## Notes
-
- Supabase db PW
	- Rdm!T*+DPk_s.3s
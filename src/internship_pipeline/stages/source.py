"""Stage: sourcing (Phase 1).

Pull public ATS JSON feeds (Greenhouse/Lever/Ashby) per ``companies.yaml``, the
SimplifyJobs ``listings.json``, and optionally JSearch; normalize to ``Job``,
dedupe by stable hash, and persist. New roles (not previously stored) are handed
to the digest stage via ``ctx.data["new_jobs"]``.

Every source is wrapped so one failing feed is logged and skipped — a bad board
token must not kill the run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..logging_config import get_logger
from ..models import Job, StageContext, StageResult
from ..sourcing.ats import fetch_company
from ..sourcing.companies import load_companies
from ..sourcing.http import build_client
from ..sourcing.jsearch import fetch_jsearch
from ..sourcing.simplify import fetch_simplify
from ..storage import get_storage

NAME = "source"

log = get_logger(__name__)

_BUNDLED_DRY_RUN_JOBS = Path(__file__).resolve().parent.parent / "fixtures" / "dry_run_jobs.json"


def _dry_run_jobs(ctx: StageContext) -> list[Job]:
    """Load fixture jobs for ``--dry-run`` — no network, no creds.

    ``date_posted`` sentinels keep the dry-run deterministic forever: ``RECENT``
    resolves to yesterday (inside the favorable window → exercises the dual-trigger)
    and ``STALE`` to 30 days ago (outside it) — fixed dates would age out.
    """
    path = ctx.settings.dry_run_jobs_file or str(_BUNDLED_DRY_RUN_JOBS)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    sentinel = {
        "RECENT": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        "STALE": (now - timedelta(days=30)).strftime("%Y-%m-%d"),
    }
    for d in data:
        d["date_posted"] = sentinel.get(d.get("date_posted"), d.get("date_posted"))
    return [Job(**d) for d in data]


def _collect(ctx: StageContext) -> tuple[list[Job], dict[str, int]]:
    """Fetch every configured source, skipping any that fail. Returns (jobs, per_source)."""
    s = ctx.settings
    jobs: list[Job] = []
    per_source: dict[str, int] = {}
    client = build_client(timeout=s.http_timeout)
    try:
        # --- ATS feeds (companies.yaml) ---
        targets = load_companies(s.companies_file, fallback="companies.example.yaml")
        for target in targets:
            label = f"{target.ats}:{target.slug}"
            try:
                got = fetch_company(client, target, max_retries=s.http_max_retries)
                jobs.extend(got)
                per_source[label] = len(got)
                log.info(
                    "sourced ATS feed",
                    extra={"run_id": ctx.run_id, "company": target.name, "count": len(got)},
                )
            except Exception as exc:  # skip-on-error per company
                log.warning(
                    "ATS feed failed; skipping",
                    extra={"run_id": ctx.run_id, "company": target.name, "error": repr(exc)},
                )

        # --- SimplifyJobs listings.json ---
        if s.enable_simplify:
            try:
                got = fetch_simplify(
                    client, s.simplify_listings_url, max_retries=s.http_max_retries
                )
                jobs.extend(got)
                per_source["simplify"] = len(got)
                log.info("sourced simplify", extra={"run_id": ctx.run_id, "count": len(got)})
            except Exception as exc:
                log.warning(
                    "simplify fetch failed; skipping",
                    extra={"run_id": ctx.run_id, "error": repr(exc)},
                )

        # --- JSearch (optional, gated) ---
        if s.enable_jsearch:
            if not s.rapidapi_key:
                log.info(
                    "jsearch enabled but RAPIDAPI_KEY unset; skipping",
                    extra={"run_id": ctx.run_id},
                )
            else:
                try:
                    got = fetch_jsearch(
                        client,
                        host=s.jsearch_host,
                        key=s.rapidapi_key,
                        query=s.jsearch_query,
                        num_pages=s.jsearch_pages,
                        max_retries=s.http_max_retries,
                    )
                    jobs.extend(got)
                    per_source["jsearch"] = len(got)
                    log.info("sourced jsearch", extra={"run_id": ctx.run_id, "count": len(got)})
                except Exception as exc:
                    log.warning(
                        "jsearch fetch failed; skipping",
                        extra={"run_id": ctx.run_id, "error": repr(exc)},
                    )
    finally:
        client.close()
    return jobs, per_source


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    if ctx.settings.dry_run:
        jobs = _dry_run_jobs(ctx)
        per_source = {"dry_run_fixture": len(jobs)}
        log.info("dry-run: sourcing from bundled fixtures",
                 extra={"run_id": ctx.run_id, "count": len(jobs)})
    else:
        jobs, per_source = _collect(ctx)

    # Dedupe within this run (a role can appear in several feeds).
    seen: set[str] = set()
    deduped: list[Job] = []
    for job in jobs:
        key = job.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    storage = get_storage(ctx.settings)
    try:
        result = storage.upsert_jobs(deduped)
    finally:
        storage.close()

    ctx.data["new_jobs"] = result.new
    ctx.data["jobs_total"] = len(deduped)
    counts = {"jobs_sourced": len(deduped), "jobs_new": result.new_count}
    log.info(
        "stage done",
        extra={"run_id": ctx.run_id, "stage": NAME, "per_source": per_source, **counts},
    )
    return StageResult(name=NAME, counts=counts, notes=f"new={result.new_count}")


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

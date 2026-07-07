"""Stage: sourcing (Phase 1).

Pull public ATS JSON feeds (Greenhouse/Lever/Ashby) per ``companies.yaml``, the
SimplifyJobs-format ``listings.json`` feeds (SimplifyJobs + same-format forks such
as vanshb03/Summer2027-Internships), curated README internship tables (e.g.
negarprh/Canadian-Tech-Internships-2026), and optionally JSearch; normalize to
``Job``, dedupe by stable hash, and persist. New roles (not previously stored) are
handed to the digest stage via ``ctx.data["new_jobs"]``.

Every source is wrapped so one failing feed is logged and skipped — a bad board
token must not kill the run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..logging_config import get_logger
from ..models import DATA_JOBS_TOTAL, DATA_NEW_JOBS, Job, StageContext, StageResult
from ..sourcing.ats import fetch_company
from ..sourcing.companies import load_companies
from ..sourcing.github_readme import fetch_readme_internships
from ..sourcing.http import build_client
from ..sourcing.jsearch import fetch_jsearch
from ..sourcing.simplify import fetch_simplify
from ..sourcing.util import repo_slug

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


def _collect(ctx: StageContext) -> tuple[list[Job], dict[str, int], list[str]]:
    """Fetch every configured source, skipping any that fail.

    Returns ``(jobs, per_source, failed_sources)`` — ``failed_sources`` is the
    label of every source that raised, so a persistent outage (a renamed board
    token, a feed that moved) is visible in the stage counts/digest instead of
    silently reducing job volume with only a WARNING buried in the logs.
    """
    s = ctx.settings
    jobs: list[Job] = []
    per_source: dict[str, int] = {}
    failed: list[str] = []
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
                failed.append(label)
                log.warning(
                    "ATS feed failed; skipping",
                    extra={"run_id": ctx.run_id, "company": target.name, "error": repr(exc)},
                )

        # --- SimplifyJobs-format listings.json feeds (SimplifyJobs + forks) ---
        if s.enable_simplify:
            for url in [s.simplify_listings_url, *s.extra_listings_url_list]:
                label = f"listings:{repo_slug(url)}"
                try:
                    got = fetch_simplify(client, url, max_retries=s.http_max_retries)
                    jobs.extend(got)
                    per_source[label] = len(got)
                    log.info(
                        "sourced listings feed",
                        extra={"run_id": ctx.run_id, "feed": label, "count": len(got)},
                    )
                except Exception as exc:  # skip-on-error per feed
                    failed.append(label)
                    log.warning(
                        "listings fetch failed; skipping",
                        extra={"run_id": ctx.run_id, "feed": label, "error": repr(exc)},
                    )

        # --- Curated README internship tables (repos with no JSON feed) ---
        if s.enable_github_readme:
            for url in s.github_readme_url_list:
                slug = repo_slug(url)
                # Label includes the filename: one repo can hold several lists
                # (e.g. README.md + README-2027.md).
                label = f"readme:{slug}:{Path(url).stem}"
                try:
                    got = fetch_readme_internships(
                        client, url, source=slug, max_retries=s.http_max_retries
                    )
                    jobs.extend(got)
                    per_source[label] = len(got)
                    log.info(
                        "sourced readme table",
                        extra={"run_id": ctx.run_id, "feed": label, "count": len(got)},
                    )
                except Exception as exc:  # skip-on-error per feed
                    failed.append(label)
                    log.warning(
                        "readme fetch failed; skipping",
                        extra={"run_id": ctx.run_id, "feed": label, "error": repr(exc)},
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
                    failed.append("jsearch")
                    log.warning(
                        "jsearch fetch failed; skipping",
                        extra={"run_id": ctx.run_id, "error": repr(exc)},
                    )
    finally:
        client.close()
    return jobs, per_source, failed


def run(ctx: StageContext) -> StageResult:
    log.info("stage start", extra={"run_id": ctx.run_id, "stage": NAME})
    failed: list[str] = []
    if ctx.settings.dry_run:
        jobs = _dry_run_jobs(ctx)
        per_source = {"dry_run_fixture": len(jobs)}
        log.info("dry-run: sourcing from bundled fixtures",
                 extra={"run_id": ctx.run_id, "count": len(jobs)})
    else:
        jobs, per_source, failed = _collect(ctx)

    # Dedupe within this run (a role can appear in several feeds).
    seen: set[str] = set()
    deduped: list[Job] = []
    for job in jobs:
        key = job.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    result = ctx.get_storage().upsert_jobs(deduped)

    ctx.data[DATA_NEW_JOBS] = result.new
    ctx.data[DATA_JOBS_TOTAL] = len(deduped)
    counts = {
        "jobs_sourced": len(deduped),
        "jobs_new": result.new_count,
        "sources_failed": len(failed),
    }
    # ok=False only when EVERY attempted source failed (per_source empty, failed
    # non-empty) — a lone bad board token is expected/tolerated (per module
    # docstring) and stays a plain WARNING; a persistent outage across the whole
    # run (renamed token, feed moved, network down) is the thing worth flipping
    # the run's status over.
    ok = not (failed and not per_source)
    if not ok:
        log.error(
            "every configured source failed this run",
            extra={"run_id": ctx.run_id, "failed_sources": failed},
        )
    log.info(
        "stage done",
        extra={"run_id": ctx.run_id, "stage": NAME, "per_source": per_source, **counts},
    )
    return StageResult(
        name=NAME, counts=counts, notes=f"new={result.new_count}, failed={failed}", ok=ok,
    )


if __name__ == "__main__":
    from ..run_daily import run_single

    raise SystemExit(run_single(NAME))

"""Supabase (Postgres) storage — the primary backend.

Talks to Supabase's PostgREST API over httpx (already a dependency), so no extra
SDK is needed. Run ``storage/sql/postgres.sql`` once in the Supabase SQL editor to
create the tables (see ACTIONS_FOR_PAUL.md). Uses the service-role key.

New-vs-seen is computed the same way as SQLite: diff against ``existing_keys``,
INSERT the new ones, PATCH ``last_seen_at`` on the rest.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from ..logging_config import get_logger
from ..models import Job, RunRecord
from .base import Storage, UpsertResult, chunked

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore(Storage):
    def __init__(self, url: str, key: str, *, timeout: float = 20.0) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )

    def _row(self, job: Job, now: str) -> dict:
        return {
            "dedupe_key": job.dedupe_key(),
            "company_name": job.company_name,
            "title": job.title,
            "url": job.url,
            "locations": job.locations,
            "date_posted": job.date_posted,
            "active": job.active,
            "source": job.source,
            "source_feed": job.source_feed.value if job.source_feed else None,
            "first_seen_at": now,
            "last_seen_at": now,
        }

    def existing_keys(self, keys: Iterable[str]) -> set[str]:
        key_list = list(keys)
        if not key_list:
            return set()
        found: set[str] = set()
        for chunk in chunked(key_list, 100):
            in_list = "(" + ",".join(chunk) + ")"
            resp = self.client.get(
                f"{self.base}/jobs",
                params={"select": "dedupe_key", "dedupe_key": f"in.{in_list}"},
            )
            resp.raise_for_status()
            found.update(row["dedupe_key"] for row in resp.json())
        return found

    def upsert_jobs(self, jobs: list[Job]) -> UpsertResult:
        if not jobs:
            return UpsertResult()
        now = _now()
        existing = self.existing_keys(j.dedupe_key() for j in jobs)
        # Dedupe within this batch while splitting into new vs seen.
        new: list[Job] = []
        seen_keys: list[str] = []
        batch_seen: set[str] = set()
        for job in jobs:
            key = job.dedupe_key()
            if key in batch_seen:
                continue
            batch_seen.add(key)
            if key in existing:
                seen_keys.append(key)
            else:
                new.append(job)

        if new:
            resp = self.client.post(
                f"{self.base}/jobs",
                params={"on_conflict": "dedupe_key"},
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                json=[self._row(j, now) for j in new],
            )
            resp.raise_for_status()
        if seen_keys:
            for chunk in chunked(seen_keys, 100):
                in_list = "(" + ",".join(chunk) + ")"
                resp = self.client.patch(
                    f"{self.base}/jobs",
                    params={"dedupe_key": f"in.{in_list}"},
                    headers={"Prefer": "return=minimal"},
                    json={"last_seen_at": now},
                )
                resp.raise_for_status()
        return UpsertResult(new=new, seen=len(seen_keys))

    def record_run(self, run: RunRecord) -> None:
        body = {
            "run_id": run.run_id,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "status": run.status,
            "counts": run.counts,
            "errors": run.errors,
        }
        resp = self.client.post(
            f"{self.base}/runs",
            params={"on_conflict": "run_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=body,
        )
        resp.raise_for_status()

    def close(self) -> None:
        self.client.close()

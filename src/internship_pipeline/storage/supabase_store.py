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
from typing import Optional

import httpx

from ..logging_config import get_logger
from ..models import Application, Job, Outreach, RunRecord
from .base import Storage, UpsertResult, chunked, suppression_matches

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

    def save_application(self, app: Application) -> None:
        now = _now()
        body = {
            "dedupe_key": app.dedupe_key,
            "company_name": app.company_name,
            "title": app.title,
            "url": app.url,
            "fit_score": app.fit_score,
            "keywords": app.keywords,
            "tailored_resume_path": app.tailored_resume_path,
            "tailored_resume_yaml": app.tailored_resume_yaml,
            "drafted_answers": app.drafted_answers,
            "human_review": app.human_review,
            "status": app.status,
            "updated_at": now,
        }
        # created_at defaults on insert; on conflict we merge without overwriting it.
        resp = self.client.post(
            f"{self.base}/applications",
            params={"on_conflict": "dedupe_key"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=body,
        )
        resp.raise_for_status()

    def get_application(self, dedupe_key: str) -> Optional[Application]:
        resp = self.client.get(
            f"{self.base}/applications",
            params={"select": "*", "dedupe_key": f"eq.{dedupe_key}", "limit": "1"},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        return Application(
            dedupe_key=row["dedupe_key"],
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            fit_score=row.get("fit_score", 0.0),
            keywords=row.get("keywords") or [],
            tailored_resume_path=row.get("tailored_resume_path"),
            tailored_resume_yaml=row.get("tailored_resume_yaml"),
            drafted_answers=row.get("drafted_answers") or {},
            human_review=bool(row.get("human_review")),
            status=row.get("status", "pending_review"),
        )

    # --- Phase 3: outreach + suppression list ---

    def save_outreach(self, outreach: Outreach) -> None:
        now = _now()
        body = {
            "outreach_id": outreach.outreach_id,
            "dedupe_key": outreach.dedupe_key,
            "company_name": outreach.company_name,
            "title": outreach.title,
            "url": outreach.url,
            "channel": outreach.channel,
            "contact_name": outreach.contact_name,
            "contact_email": outreach.contact_email,
            "contact_title": outreach.contact_title,
            "contact_source": outreach.contact_source,
            "contact_confidence": outreach.contact_confidence,
            "contact_verified": outreach.contact_verified,
            "contact_note": outreach.contact_note,
            "subject": outreach.subject,
            "body": outreach.body,
            "status": outreach.status,
            "suppressed": outreach.suppressed,
            "human_review": outreach.human_review,
            "used_llm": outreach.used_llm,
            "sent_at": outreach.sent_at,
            "provider_message_id": outreach.provider_message_id,
            "updated_at": now,
        }
        # created_at defaults on insert; on conflict we merge without overwriting it.
        resp = self.client.post(
            f"{self.base}/outreach",
            params={"on_conflict": "outreach_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=body,
        )
        resp.raise_for_status()

    @staticmethod
    def _row_to_outreach(row: dict) -> Outreach:
        return Outreach(
            outreach_id=row["outreach_id"],
            dedupe_key=row["dedupe_key"],
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            channel=row["channel"],
            contact_name=row.get("contact_name"),
            contact_email=row.get("contact_email"),
            contact_title=row.get("contact_title"),
            contact_source=row.get("contact_source", "none"),
            contact_confidence=row.get("contact_confidence"),
            contact_verified=bool(row.get("contact_verified")),
            contact_note=row.get("contact_note"),
            subject=row.get("subject"),
            body=row.get("body") or "",
            status=row.get("status", "pending_review"),
            suppressed=bool(row.get("suppressed")),
            human_review=bool(row.get("human_review")),
            used_llm=bool(row.get("used_llm")),
            sent_at=row.get("sent_at"),
            provider_message_id=row.get("provider_message_id"),
        )

    def get_outreach(self, outreach_id: str) -> Optional[Outreach]:
        resp = self.client.get(
            f"{self.base}/outreach",
            params={"select": "*", "outreach_id": f"eq.{outreach_id}", "limit": "1"},
        )
        resp.raise_for_status()
        rows = resp.json()
        return None if not rows else self._row_to_outreach(rows[0])

    def list_outreach(self, status: Optional[str] = None) -> list[Outreach]:
        params = {"select": "*", "order": "created_at.desc"}
        if status is not None:
            params["status"] = f"eq.{status}"
        resp = self.client.get(f"{self.base}/outreach", params=params)
        resp.raise_for_status()
        return [self._row_to_outreach(r) for r in resp.json()]

    def add_suppression(self, entry: str, reason: Optional[str] = None) -> None:
        normalized = (entry or "").strip().lower()
        if not normalized:
            return
        resp = self.client.post(
            f"{self.base}/suppressions",
            params={"on_conflict": "entry"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"entry": normalized, "reason": reason, "created_at": _now()},
        )
        resp.raise_for_status()

    def list_suppressions(self) -> list[str]:
        resp = self.client.get(f"{self.base}/suppressions", params={"select": "entry"})
        resp.raise_for_status()
        return [r["entry"] for r in resp.json()]

    def is_suppressed(self, email: str) -> bool:
        return suppression_matches(email, self.list_suppressions())

    def close(self) -> None:
        self.client.close()

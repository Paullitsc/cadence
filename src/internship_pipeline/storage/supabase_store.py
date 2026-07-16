"""Supabase (Postgres) storage — the primary backend.

Talks to Supabase's PostgREST API over httpx (already a dependency), so no extra
SDK is needed. Run ``storage/sql/postgres.sql`` once in the Supabase SQL editor to
create the tables (see ACTIONS_FOR_PAUL.md). Uses the service-role key.

New-vs-seen, and what refreshes on a re-seen job, is computed the same way as
SQLite: last_seen_at/active/title/locations update; company_name, url,
date_posted, source, source_feed and first_seen_at are frozen at whatever they
were first stored as. One snapshot query + one batched upsert call.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..logging_config import get_logger
from ..models import Application, CvCacheEntry, Job, Outreach, RunRecord
from ..networking.models import Person
from .base import Storage, UpsertResult, chunked, suppression_matches

log = get_logger(__name__)

# PostgREST caps a single response at 1000 rows by default (server-side
# `db-max-rows`). Left unhandled, every unbounded `list_*` here silently
# truncates once a table crosses that size — reads keep succeeding, they just
# quietly drop rows. Paginate with the `Range` header instead of trusting a
# single GET to return everything.
_PAGE_SIZE = 1000

# Columns a re-seen job's upsert must echo back UNCHANGED (frozen at whatever
# they were on first insert) — everything else (last_seen_at, active, title,
# locations) refreshes. Matches SQLiteStore.upsert_jobs's UPDATE column list.
_FROZEN_ON_REFRESH: tuple[str, ...] = (
    "company_name", "url", "date_posted", "source", "source_feed", "first_seen_at",
)


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

    def _get_all(self, path: str, params: dict) -> list[dict]:
        """GET every row from a PostgREST endpoint, paginating past the 1000-row cap.

        Uses the ``Range`` header (PostgREST's pagination mechanism) rather than
        trusting a plain GET to return the full table. Stops once a page comes
        back short of ``_PAGE_SIZE`` — no dependency on a ``Content-Range`` total.
        """
        rows: list[dict] = []
        offset = 0
        while True:
            resp = self.client.get(
                f"{self.base}/{path}",
                params=params,
                headers={"Range-Unit": "items", "Range": f"{offset}-{offset + _PAGE_SIZE - 1}"},
            )
            resp.raise_for_status()
            page = resp.json()
            rows.extend(page)
            if len(page) < _PAGE_SIZE:
                return rows
            offset += _PAGE_SIZE

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

    def stale_job_keys(self, cutoff_iso: str) -> set[str]:
        rows = self._get_all(
            "jobs", {"select": "dedupe_key", "last_seen_at": f"lt.{cutoff_iso}"}
        )
        return {row["dedupe_key"] for row in rows}

    def _existing_snapshot(self, keys: list[str]) -> dict[str, dict]:
        """``_FROZEN_ON_REFRESH`` column values for whichever of ``keys`` already
        exist — both the new-vs-seen signal (membership) and what a re-seen row's
        upsert must echo back unchanged, since a partial-column PostgREST upsert
        would otherwise NULL out (and violate NOT NULL on) the omitted columns.
        """
        cols = "dedupe_key," + ",".join(_FROZEN_ON_REFRESH)
        found: dict[str, dict] = {}
        for chunk in chunked(keys, 100):
            in_list = "(" + ",".join(chunk) + ")"
            resp = self.client.get(
                f"{self.base}/jobs",
                params={"select": cols, "dedupe_key": f"in.{in_list}"},
            )
            resp.raise_for_status()
            found.update({row["dedupe_key"]: row for row in resp.json()})
        return found

    def upsert_jobs(self, jobs: list[Job]) -> UpsertResult:
        if not jobs:
            return UpsertResult()
        now = _now()
        # Dedupe within this batch (source.py already dedupes per run; this is a
        # defensive backstop — Postgres rejects an upsert that targets the same
        # conflict key twice in one statement).
        unique: dict[str, Job] = {}
        for job in jobs:
            unique.setdefault(job.dedupe_key(), job)

        existing = self._existing_snapshot(list(unique))

        new: list[Job] = []
        seen = 0
        rows: list[dict] = []
        for key, job in unique.items():
            row = self._row(job, now)
            snapshot = existing.get(key)
            if snapshot is None:
                new.append(job)
            else:
                seen += 1
                row.update({col: snapshot[col] for col in _FROZEN_ON_REFRESH})
            rows.append(row)

        for chunk in chunked(rows, 500):
            resp = self.client.post(
                f"{self.base}/jobs",
                params={"on_conflict": "dedupe_key"},
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                json=chunk,
            )
            resp.raise_for_status()
        return UpsertResult(new=new, seen=seen)

    def get_job(self, dedupe_key: str) -> Optional[Job]:
        resp = self.client.get(
            f"{self.base}/jobs",
            params={"select": "*", "dedupe_key": f"eq.{dedupe_key}", "limit": "1"},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        return Job(
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            locations=row.get("locations") or [],
            date_posted=row.get("date_posted"),
            active=bool(row.get("active", True)),
            source=row.get("source"),
            source_feed=row.get("source_feed"),
        )

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
            "cv_drive_link": app.cv_drive_link,
            "drafted_answers": app.drafted_answers,
            "recommended_bullets": app.recommended_bullets,
            "final_bullets": app.final_bullets,
            "reviewed_at": app.reviewed_at,
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

    @staticmethod
    def _row_to_application(row: dict) -> Application:
        return Application(
            dedupe_key=row["dedupe_key"],
            company_name=row["company_name"],
            title=row["title"],
            url=row["url"],
            fit_score=row.get("fit_score", 0.0),
            keywords=row.get("keywords") or [],
            tailored_resume_path=row.get("tailored_resume_path"),
            tailored_resume_yaml=row.get("tailored_resume_yaml"),
            cv_drive_link=row.get("cv_drive_link"),
            drafted_answers=row.get("drafted_answers") or {},
            recommended_bullets=row.get("recommended_bullets") or [],
            final_bullets=row.get("final_bullets") or [],
            reviewed_at=row.get("reviewed_at"),
            human_review=bool(row.get("human_review")),
            status=row.get("status", "pending_review"),
        )

    def get_application(self, dedupe_key: str) -> Optional[Application]:
        resp = self.client.get(
            f"{self.base}/applications",
            params={"select": "*", "dedupe_key": f"eq.{dedupe_key}", "limit": "1"},
        )
        resp.raise_for_status()
        rows = resp.json()
        return None if not rows else self._row_to_application(rows[0])

    def list_applications(self, status: Optional[str] = None) -> list[Application]:
        params = {"select": "*", "order": "fit_score.desc"}
        if status is not None:
            params["status"] = f"eq.{status}"
        return [self._row_to_application(r) for r in self._get_all("applications", params)]

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
            "gmail_draft_id": outreach.gmail_draft_id,
            "gmail_draft_link": outreach.gmail_draft_link,
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
            gmail_draft_id=row.get("gmail_draft_id"),
            gmail_draft_link=row.get("gmail_draft_link"),
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
        return [self._row_to_outreach(r) for r in self._get_all("outreach", params)]

    # --- Phase 5: cross-run CV cache ---

    def get_cv_cache(self, cache_key: str) -> Optional[CvCacheEntry]:
        resp = self.client.get(
            f"{self.base}/cv_cache",
            params={"select": "*", "cache_key": f"eq.{cache_key}", "limit": "1"},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        return self._row_to_cv_cache(rows[0])

    @staticmethod
    def _row_to_cv_cache(row: dict) -> CvCacheEntry:
        return CvCacheEntry(
            cache_key=row["cache_key"],
            tailored_resume_yaml=row.get("tailored_resume_yaml") or "",
            cv_drive_link=row.get("cv_drive_link"),
            drive_file_id=row.get("drive_file_id"),
            pdf_path=row.get("pdf_path"),
            recommended_bullets=row.get("recommended_bullets") or [],
        )

    def list_cv_cache(self) -> list[CvCacheEntry]:
        rows = self._get_all("cv_cache", {"select": "*", "order": "created_at.asc"})
        return [self._row_to_cv_cache(r) for r in rows]

    def save_cv_cache(self, entry: CvCacheEntry) -> None:
        resp = self.client.post(
            f"{self.base}/cv_cache",
            params={"on_conflict": "cache_key"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={
                "cache_key": entry.cache_key,
                "tailored_resume_yaml": entry.tailored_resume_yaml,
                "cv_drive_link": entry.cv_drive_link,
                "drive_file_id": entry.drive_file_id,
                "pdf_path": entry.pdf_path,
                "recommended_bullets": entry.recommended_bullets,
            },
        )
        resp.raise_for_status()

    # --- Phase 6: networking campaign people ---

    def save_person(self, person: Person) -> None:
        body = {
            "person_id": person.person_id,
            "campaign": person.campaign,
            "company_name": person.company_name,
            "company_domain": person.company_domain,
            "company_website": person.company_website,
            "company_linkedin": person.company_linkedin,
            "company_blurb": person.company_blurb,
            "tier": person.tier,
            "name": person.name,
            "role": person.role,
            "linkedin_url": person.linkedin_url,
            "email": person.email,
            "status": person.status,
            "status_changed_at": person.status_changed_at,
            "draft_kind": person.draft_kind,
            "draft_subject": person.draft_subject,
            "draft_body": person.draft_body,
            "used_llm": person.used_llm,
            "updated_at": _now(),
        }
        # created_at defaults on insert; on conflict we merge without overwriting it.
        resp = self.client.post(
            f"{self.base}/people",
            params={"on_conflict": "person_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=body,
        )
        resp.raise_for_status()

    @staticmethod
    def _row_to_person(row: dict) -> Person:
        return Person(
            person_id=row["person_id"],
            campaign=row.get("campaign") or "default",
            company_name=row["company_name"],
            company_domain=row.get("company_domain"),
            company_website=row.get("company_website"),
            company_linkedin=row.get("company_linkedin"),
            company_blurb=row.get("company_blurb") or "",
            tier=row.get("tier", 2),
            name=row.get("name"),
            role=row.get("role"),
            linkedin_url=row.get("linkedin_url"),
            email=row.get("email"),
            status=row.get("status", "queued"),
            status_changed_at=row.get("status_changed_at"),
            draft_kind=row.get("draft_kind"),
            draft_subject=row.get("draft_subject"),
            draft_body=row.get("draft_body") or "",
            used_llm=bool(row.get("used_llm")),
        )

    def get_person(self, person_id: str) -> Optional[Person]:
        resp = self.client.get(
            f"{self.base}/people",
            params={"select": "*", "person_id": f"eq.{person_id}", "limit": "1"},
        )
        resp.raise_for_status()
        rows = resp.json()
        return None if not rows else self._row_to_person(rows[0])

    def list_people(self, status: Optional[str] = None) -> list[Person]:
        params = {"select": "*", "order": "tier.asc,company_name.asc,person_id.asc"}
        if status is not None:
            params["status"] = f"eq.{status}"
        return [self._row_to_person(r) for r in self._get_all("people", params)]

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
        return [r["entry"] for r in self._get_all("suppressions", {"select": "entry"})]

    def is_suppressed(self, email: str) -> bool:
        return suppression_matches(email, self.list_suppressions())

    def close(self) -> None:
        self.client.close()

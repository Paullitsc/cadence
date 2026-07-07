"""SupabaseStore.upsert_jobs must match SQLiteStore's refresh contract exactly:
on a re-seen job, last_seen_at/active/title/locations update but company_name,
url, date_posted, source, source_feed, and first_seen_at stay frozen at
whatever they were on first insert. Uses httpx.MockTransport (no real network)
to simulate a Postgres table and assert on the upsert payload actually sent.
"""

from __future__ import annotations

import json

import httpx

from internship_pipeline.models import Job, JobSource
from internship_pipeline.storage.supabase_store import SupabaseStore


def _job(url: str, title: str = "Intern", company: str = "Acme") -> Job:
    return Job(
        company_name=company,
        title=title,
        url=url,
        locations=["Remote"],
        date_posted="2026-01-01",
        source="greenhouse:acme",
        source_feed=JobSource.GREENHOUSE,
    )


def test_new_job_upsert_sends_full_row():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])  # nothing exists yet
        posted.append(json.loads(request.content))
        return httpx.Response(201, json=[])

    store = SupabaseStore("https://example.supabase.co", "key")
    store.client = httpx.Client(transport=httpx.MockTransport(handler), headers=store.client.headers)

    result = store.upsert_jobs([_job("https://x/1")])
    assert result.new_count == 1
    assert result.seen == 0
    row = posted[0][0]
    assert row["company_name"] == "Acme"
    assert row["title"] == "Intern"
    assert row["first_seen_at"] == row["last_seen_at"]


def test_seen_job_refreshes_mutable_fields_but_freezes_the_rest():
    old_snapshot = {
        "dedupe_key": _job("https://x/1").dedupe_key(),
        "company_name": "Old Name Inc",
        "url": "https://x/1-old",
        "date_posted": "2020-01-01",
        "source": "greenhouse:old",
        "source_feed": "greenhouse",
        "first_seen_at": "2020-01-01T00:00:00+00:00",
    }
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[old_snapshot])
        posted.append(json.loads(request.content))
        return httpx.Response(201, json=[])

    store = SupabaseStore("https://example.supabase.co", "key")
    store.client = httpx.Client(transport=httpx.MockTransport(handler), headers=store.client.headers)

    # Same URL (-> same dedupe key) but the incoming Job has different
    # title/active/locations AND different company_name/date_posted/source —
    # only the former set should make it into the upsert payload.
    result = store.upsert_jobs([_job("https://x/1", title="Senior Intern", company="New Name Inc")])
    assert result.new_count == 0
    assert result.seen == 1

    row = posted[0][0]
    assert row["title"] == "Senior Intern"  # refreshed
    assert row["locations"] == ["Remote"]  # refreshed
    # Frozen at the pre-existing stored values, not the incoming Job's:
    assert row["company_name"] == "Old Name Inc"
    assert row["url"] == "https://x/1-old"
    assert row["date_posted"] == "2020-01-01"
    assert row["source"] == "greenhouse:old"
    assert row["first_seen_at"] == "2020-01-01T00:00:00+00:00"


def test_stale_job_keys_filters_by_last_seen_lt_cutoff():
    seen_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json=[{"dedupe_key": "abc123"}])

    store = SupabaseStore("https://example.supabase.co", "key")
    store.client = httpx.Client(transport=httpx.MockTransport(handler), headers=store.client.headers)

    keys = store.stale_job_keys("2026-01-01T00:00:00+00:00")
    assert keys == {"abc123"}
    assert seen_params[0]["last_seen_at"] == "lt.2026-01-01T00:00:00+00:00"

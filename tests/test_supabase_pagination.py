"""SupabaseStore._get_all must paginate past PostgREST's 1000-row page cap.

Uses httpx.MockTransport (no real network) to simulate a table with more rows
than one page holds, and asserts every list_* method follows the Range header
across pages instead of silently truncating at the first response.
"""

from __future__ import annotations

import httpx

from internship_pipeline.storage.supabase_store import _PAGE_SIZE, SupabaseStore


def _mocked_store(handler) -> SupabaseStore:
    store = SupabaseStore("https://example.supabase.co", "test-key")
    store.client = httpx.Client(
        transport=httpx.MockTransport(handler), headers=store.client.headers
    )
    return store


def test_get_all_follows_range_header_across_pages():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["range"])
        offset = int(request.headers["range"].split("-")[0])
        if offset == 0:
            rows = [{"entry": f"a{i}@x.com"} for i in range(_PAGE_SIZE)]
        else:
            rows = [{"entry": "last@x.com"}]
        return httpx.Response(200, json=rows)

    store = _mocked_store(handler)
    entries = store.list_suppressions()

    assert len(entries) == _PAGE_SIZE + 1
    assert entries[-1] == "last@x.com"
    assert len(calls) == 2  # first page full -> fetched a second


def test_get_all_stops_after_a_short_page():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"entry": "only@x.com"}])

    store = _mocked_store(handler)
    entries = store.list_suppressions()

    assert entries == ["only@x.com"]

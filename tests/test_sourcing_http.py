"""get_json/get_text/post_json share one retrying request helper (_request).

Uses httpx.MockTransport (no real network) to verify: the right HTTP verb is
sent, the right part of the response is extracted, transient failures (5xx)
retry and eventually succeed, and non-retryable failures (4xx) fail fast.
"""

from __future__ import annotations

import httpx
import pytest

from internship_pipeline.sourcing.http import get_json, get_text, post_json


def test_get_json_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_json(client, "https://x/jobs") == {"ok": True}


def test_get_text_returns_raw_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, text="raw markdown")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_text(client, "https://x/README.md") == "raw markdown"


def test_post_json_sends_body_and_verb():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.content == b'{"q":"intern"}'
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert post_json(client, "https://x/search", json={"q": "intern"}) == {"data": []}


def test_5xx_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_json(client, "https://x/flaky", max_retries=5) == {"ok": True}
    assert calls["n"] == 3


def test_404_fails_fast_without_retrying():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        get_json(client, "https://x/missing", max_retries=5)
    assert calls["n"] == 1

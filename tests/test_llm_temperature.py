"""Regression: models that reject `temperature` (e.g. claude-sonnet-5) must not kill
the run — the wrapper retries without it and remembers for subsequent calls.
"""

from __future__ import annotations

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")  # optional extra; absent in base CI env

from internship_pipeline.config import Settings  # noqa: E402
from internship_pipeline.resume.llm import build_default_complete  # noqa: E402


def _bad_request(message: str) -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError(message, response=resp, body=None)


class _Block:
    type = "text"
    text = '{"ok": true}'


class _Response:
    content = [_Block()]


class _FakeMessages:
    def __init__(self, reject_temperature: bool):
        self.reject_temperature = reject_temperature
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_temperature and "temperature" in kwargs:
            raise _bad_request(
                "Error code: 400 - {'error': {'message': '`temperature` is deprecated for this model.'}}"
            )
        return _Response()


class _FakeClient:
    def __init__(self, reject_temperature: bool):
        self.messages = _FakeMessages(reject_temperature)


def _complete_with(monkeypatch, reject_temperature: bool):
    fake = _FakeClient(reject_temperature)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake)
    settings = Settings(_env_file=None, anthropic_api_key="k", anthropic_model="claude-sonnet-5")
    return build_default_complete(settings), fake


def test_temperature_rejection_retries_without_and_remembers(monkeypatch):
    complete, fake = _complete_with(monkeypatch, reject_temperature=True)

    assert complete([], "hi") == {"ok": True}
    # call 1: with temperature (rejected); call 2: retried without
    assert "temperature" in fake.messages.calls[0]
    assert "temperature" not in fake.messages.calls[1]

    assert complete([], "again") == {"ok": True}
    # subsequent calls skip temperature straight away (no wasted 400 per call)
    assert "temperature" not in fake.messages.calls[2]
    assert len(fake.messages.calls) == 3


def test_temperature_kept_for_models_that_accept_it(monkeypatch):
    complete, fake = _complete_with(monkeypatch, reject_temperature=False)
    assert complete([], "hi") == {"ok": True}
    assert fake.messages.calls[0]["temperature"] == 0


def test_unrelated_bad_request_still_raises(monkeypatch):
    fake = _FakeClient(reject_temperature=False)

    def _boom(**kwargs):
        raise _bad_request("Error code: 400 - max_tokens too large")

    fake.messages.create = _boom
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake)
    settings = Settings(_env_file=None, anthropic_api_key="k")
    complete = build_default_complete(settings)
    with pytest.raises(anthropic.BadRequestError):
        complete([], "hi")

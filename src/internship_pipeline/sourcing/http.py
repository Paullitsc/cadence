"""Shared HTTP: a configured httpx client and a retrying JSON GET.

External calls are wrapped with tenacity retry-with-backoff (blueprint section 5).
Only transient failures retry — transport errors and 429/5xx; a 404 (e.g. a bad
board token) fails fast so the caller can skip that source.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..logging_config import get_logger

log = get_logger(__name__)

# A polite, identifiable UA. These are public, no-auth feeds.
USER_AGENT = "internship-pipeline/1.0 (+https://github.com; daily internship sourcing)"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def build_client(timeout: float = 20.0) -> httpx.Client:
    """Construct an httpx client with sane defaults for JSON feeds."""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def is_retryable(exc: BaseException) -> bool:
    """Retry only transient failures: transport errors and 429/5xx (not 4xx)."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    max_retries: int = 3,
) -> Any:
    """GET ``url`` and return parsed JSON, retrying transient failures."""

    @retry(
        reraise=True,
        stop=stop_after_attempt(max(1, max_retries)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(is_retryable),
    )
    def _do() -> Any:
        resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    return _do()


def get_text(
    client: httpx.Client,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    max_retries: int = 3,
) -> str:
    """GET ``url`` and return the body as text, retrying transient failures."""

    @retry(
        reraise=True,
        stop=stop_after_attempt(max(1, max_retries)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(is_retryable),
    )
    def _do() -> str:
        resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.text

    return _do()


def post_json(
    client: httpx.Client,
    url: str,
    *,
    json: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    max_retries: int = 3,
) -> Any:
    """POST a JSON body to ``url`` and return parsed JSON, retrying transient failures."""

    @retry(
        reraise=True,
        stop=stop_after_attempt(max(1, max_retries)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(is_retryable),
    )
    def _do() -> Any:
        resp = client.post(url, json=json, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    return _do()

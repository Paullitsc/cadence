"""Thin Anthropic wrapper for the two Phase-2 reasoning calls (tailoring, answers).

Uses the official ``anthropic`` SDK (lazy-imported so the base install and the test
suite never need it). The model is an env-config constant (blueprint: Claude Haiku
4.5); temperature is pinned low for deterministic-ish output; the stable
master-résumé/system context is sent with ``cache_control`` so repeated calls in a
run hit the prompt cache.

The public unit is a ``CompleteFn`` — ``(system_blocks, user_text) -> dict`` — so
callers depend on a plain callable and tests inject a fake instead of the live API.

    # TODO(batch): non-urgent tailoring could go through the Batch API (50% cheaper,
    # up to 24h latency). We run synchronously so the tailored résumé is ready in the
    # same morning digest; revisit if daily volume grows. See ACTIONS_FOR_PAUL.md.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from ..config import Settings
from ..logging_config import get_logger

log = get_logger(__name__)

# (system content blocks, user text) -> parsed JSON object
CompleteFn = Callable[[list[dict], str], dict]

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_object(text: str) -> dict:
    """Best-effort parse of a JSON object from an LLM text response.

    Tolerates ``` fences and surrounding prose by extracting the first balanced
    ``{...}`` span. Raises ``ValueError`` if nothing parseable is found.
    """
    candidates = [text.strip()]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in model response")


def _extract_text(response: object) -> str:
    """Concatenate text blocks from an anthropic Message response."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def build_default_complete(settings: Settings) -> Optional[CompleteFn]:
    """Build a live-Anthropic ``CompleteFn``, or ``None`` if unavailable.

    Returns ``None`` (so callers fall back to deterministic behavior) when there is
    no API key or the ``anthropic`` SDK is not installed — the pipeline must run
    end-to-end with zero credentials.
    """
    if not settings.anthropic_api_key:
        log.info("ANTHROPIC_API_KEY unset; LLM steps run in deterministic fallback mode")
        return None
    try:
        import anthropic  # heavy / optional; lazy import
    except ImportError:
        log.warning("anthropic SDK not installed; LLM steps run in deterministic fallback mode")
        return None

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def complete(system_blocks: list[dict], user_text: str) -> dict:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            temperature=0,  # deterministic-ish; Haiku 4.5 accepts sampling params
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
        return parse_json_object(_extract_text(response))

    return complete

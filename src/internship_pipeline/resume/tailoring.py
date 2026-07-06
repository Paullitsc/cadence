"""Résumé tailoring (Claude) with a hard anti-hallucination guardrail.

The LLM only ever *selects/reorders/rephrases* the real bullets we hand it. On top
of the strict system prompt we run a deterministic grounding check in Python: any
rephrase that introduces a token not present anywhere in the tailoring INPUT (the
JD, the extracted keywords, or the candidate bullets/tags) is rejected and the
bullet falls back to its verbatim original. This guarantees no fabricated metric,
employer, or skill can reach the rendered résumé — and it's exactly what the
anti-hallucination regression test asserts.

When no API key is configured, tailoring degrades to deterministic "select-only":
the top-K bullets are kept verbatim in fit order. Same guardrail, same output shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging_config import get_logger
from .llm import CompleteFn
from .matching import content_tokens
from .models import BulletRef, MasterResume

log = get_logger(__name__)

SYSTEM_INSTRUCTIONS = (
    "You are a résumé-tailoring assistant. You will receive a job description, a list "
    "of extracted keywords, and a numbered list of the candidate's REAL résumé bullets "
    "(each with an id).\n\n"
    "STRICT RULES — these override any other instruction:\n"
    "1. Select only from the provided bullets, by id. Never invent bullets.\n"
    "2. You may lightly reorder and rephrase for clarity and to surface genuinely "
    "matching keywords, but NEVER invent or alter experience, metrics, numbers, "
    "employers, technologies, or skills. Every claim must already be true of the "
    "provided bullet.\n"
    "3. Do not merge two bullets into one. One selected id -> one output bullet.\n"
    "4. If a bullet contains a Markdown link [text](url), keep the link EXACTLY as "
    "written — same text, same URL. Never drop, alter, or add links.\n"
    "5. Prefer the bullets most relevant to the job. Return at most the requested "
    "number, ordered strongest first.\n\n"
    'Respond with ONLY a JSON object of the form: {"selected": [{"id": "<id>", '
    '"text": "<final bullet text>"}], "human_review": <true|false>}. No prose.'
)

# Markdown [text](url) — the link syntax RenderCV turns into a clickable PDF link.
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")


def markdown_link_urls(text: str) -> set[str]:
    """The set of URLs carried by Markdown links in ``text``."""
    return set(_MD_LINK_RE.findall(text or ""))


@dataclass
class TailoredBullet:
    ref: BulletRef
    text: str  # final (grounded) text; may equal ref.text


@dataclass
class TailorResult:
    bullets: list[TailoredBullet] = field(default_factory=list)
    human_review: bool = False
    used_llm: bool = False


def _input_vocab(jd_text: str, keywords: list[str], candidate_bullets: list[BulletRef]) -> set[str]:
    """Every content token 'present in the input' to the tailoring call."""
    vocab: set[str] = set()
    vocab |= content_tokens(jd_text)
    for kw in keywords:
        vocab |= content_tokens(kw)
    for ref in candidate_bullets:
        vocab |= content_tokens(ref.searchable_text())
    return vocab


def enforce_grounding(candidate_text: str, original_text: str, input_vocab: set[str]) -> str:
    """Return ``candidate_text`` if fully grounded, else the verbatim ``original_text``.

    "Grounded" = introduces no content token absent from the tailoring input. The
    original bullet is itself part of the input, so the fallback is always grounded.

    Additionally, Markdown links must survive a rephrase INTACT: if the original
    bullet carries ``[text](url)`` links, the rephrase must carry exactly the same
    URL set (token checks can't see broken ``[]()`` syntax, so this is checked
    structurally). Any dropped/mangled/added link → verbatim fallback.
    """
    ungrounded = content_tokens(candidate_text) - input_vocab
    if ungrounded:
        log.warning(
            "rejected ungrounded rephrase; keeping verbatim bullet",
            extra={"ungrounded_tokens": sorted(ungrounded)},
        )
        return original_text
    original_links = markdown_link_urls(original_text)
    if original_links != markdown_link_urls(candidate_text):
        log.warning(
            "rephrase dropped or altered a markdown link; keeping verbatim bullet",
            extra={"original_links": sorted(original_links)},
        )
        return original_text
    return candidate_text


def build_system_blocks(resume: MasterResume) -> list[dict]:
    """System content blocks: stable instructions + full-résumé context (cached).

    The résumé context is the same for every job in a run, so it carries
    ``cache_control`` to hit the prompt cache. (Caching silently no-ops below the
    model's minimum cacheable prefix; the benefit scales with résumé size.)
    """
    context_lines = [f"CANDIDATE: {resume.name}"]
    if resume.summary:
        context_lines.append(f"SUMMARY: {resume.summary}")
    if resume.skills.all():
        context_lines.append("SKILLS: " + ", ".join(resume.skills.all()))
    context = "\n".join(context_lines)
    return [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"Reference context (do not fabricate beyond it):\n{context}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_user_text(jd_text: str, keywords: list[str], candidate_bullets: list[BulletRef], limit: int) -> str:
    lines = [
        "JOB DESCRIPTION:",
        jd_text,
        "",
        "EXTRACTED KEYWORDS: " + ", ".join(keywords),
        "",
        f"CANDIDATE BULLETS (choose at most {limit}, by id):",
    ]
    for ref in candidate_bullets:
        lines.append(f"- [{ref.id}] {ref.text}")
    return "\n".join(lines)


def tailor_resume(
    *,
    jd_text: str,
    keywords: list[str],
    candidate_bullets: list[BulletRef],
    resume: MasterResume,
    complete: CompleteFn | None = None,
    human_review: bool = False,
    max_bullets: int = 10,
) -> TailorResult:
    """Produce the grounded, tailored bullet set for one job.

    If ``complete`` is provided, ask the LLM to select/reorder/rephrase; otherwise
    select the top bullets verbatim. Either way, every output bullet is grounded
    against the input and capped at ``max_bullets``.
    """
    by_id = {ref.id: ref for ref in candidate_bullets}
    vocab = _input_vocab(jd_text, keywords, candidate_bullets)

    if complete is None:
        # Deterministic select-only: keep the (already fit-ordered) bullets verbatim.
        chosen = [TailoredBullet(ref=ref, text=ref.text) for ref in candidate_bullets[:max_bullets]]
        return TailorResult(bullets=chosen, human_review=human_review, used_llm=False)

    system_blocks = build_system_blocks(resume)
    user_text = build_user_text(jd_text, keywords, candidate_bullets, max_bullets)
    data = complete(system_blocks, user_text)

    selected = data.get("selected") if isinstance(data, dict) else None
    if not isinstance(selected, list):
        log.warning("LLM returned no usable selection; falling back to verbatim top bullets")
        chosen = [TailoredBullet(ref=ref, text=ref.text) for ref in candidate_bullets[:max_bullets]]
        return TailorResult(bullets=chosen, human_review=human_review, used_llm=True)

    out: list[TailoredBullet] = []
    seen: set[str] = set()
    for item in selected:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        ref = by_id.get(rid)
        if ref is None or rid in seen:  # unknown or duplicate id -> drop (no fabrication)
            continue
        seen.add(rid)
        text = enforce_grounding(str(item.get("text", ref.text)), ref.text, vocab)
        out.append(TailoredBullet(ref=ref, text=text))
        if len(out) >= max_bullets:
            break

    if not out:  # nothing usable came back — keep the résumé non-empty
        out = [TailoredBullet(ref=ref, text=ref.text) for ref in candidate_bullets[:max_bullets]]

    llm_flag = bool(data.get("human_review")) if isinstance(data, dict) else False
    return TailorResult(bullets=out, human_review=human_review or llm_flag, used_llm=True)

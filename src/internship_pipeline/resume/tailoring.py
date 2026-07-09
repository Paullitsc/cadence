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

Keyword bolding: JD keywords found in a bullet are wrapped in Markdown ``**bold**``
(rendered bold in the PDF). The LLM is instructed to do it (and to preserve any
bold already written in ``master_resume.yaml``), and ``bold_keywords`` runs as a
deterministic post-pass in both paths so the emphasis is guaranteed either way —
it only ever adds asterisks, never words, so grounding is unaffected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging_config import get_logger
from .llm import CompleteFn, resume_system_blocks
from .matching import content_tokens
from .models import BulletRef, MasterResume

log = get_logger(__name__)

SYSTEM_INSTRUCTIONS = (
    "<role>\n"
    "You are an expert technical résumé writer tailoring an internship candidate's "
    "résumé to one specific job. You will receive a job description, a list of "
    "extracted keywords, and a numbered list of the candidate's REAL résumé bullets "
    "(each with an id). Your output is read by recruiters and ATS keyword scanners.\n"
    "</role>\n\n"
    "<strict_rules>\n"
    "These override any other instruction:\n"
    "1. Select only from the provided bullets, by id. Never invent bullets.\n"
    "2. You may lightly reorder and rephrase for clarity and to surface genuinely "
    "matching keywords, but NEVER invent or alter experience, metrics, numbers, "
    "employers, technologies, or skills. Every claim must already be true of the "
    "provided bullet.\n"
    "3. Do not merge two bullets into one. One selected id -> one output bullet.\n"
    "4. If a bullet contains a Markdown link [text](url), keep the link EXACTLY as "
    "written — same text, same URL. Never drop, alter, or add links.\n"
    "5. Emphasis: keep any existing Markdown **bold** in a bullet exactly where it "
    "is. Additionally, wrap in **bold** the words/phrases already in the bullet that "
    "literally match the extracted keywords. Bolding is formatting ONLY — never add, "
    "drop, or reword anything to force a keyword match.\n"
    "</strict_rules>\n\n"
    "<selection_strategy>\n"
    "1. First identify the job's 3-5 core requirements from the description and "
    "keywords.\n"
    "2. Choose bullets that together COVER those distinct requirements. Do not stack "
    "near-duplicate bullets that prove the same skill twice when another requirement "
    "is still uncovered.\n"
    "3. When bullets are otherwise comparable, prefer the one with concrete, "
    "quantified impact.\n"
    "4. Order strongest-and-most-relevant first.\n"
    "5. FILL THE PAGE: the rendered résumé must be a full single page dense with "
    "relevant information, so return as close to the requested number of bullets "
    "as the material allows. After covering the core requirements, keep adding the "
    "next most relevant bullets (breadth of real experience beats white space). "
    "Drop a bullet only when it is clearly irrelevant to this job — never merely "
    "because it is weaker than the others; overflow is trimmed automatically at "
    "render time, but white space cannot be filled back in.\n"
    "</selection_strategy>\n\n"
    "<rephrasing_guidance>\n"
    "- Mirror the job description's exact terminology ONLY where the bullet already "
    "demonstrates that thing (e.g. say 'CI/CD' like the JD does if the bullet "
    "genuinely describes it). Never re-badge unrelated work.\n"
    "- Start each bullet with a strong action verb; keep it tight (about 1-2 lines).\n"
    "- If the original wording is already strong, keep it verbatim — rephrase only "
    "when it clarifies or surfaces a genuine keyword match.\n"
    "</rephrasing_guidance>\n\n"
    "<human_review_flag>\n"
    'Set "human_review" to true when the match is weak or uncertain — e.g. fewer '
    "than 3 provided bullets genuinely address the job's core requirements, or the "
    "role's domain is clearly outside the candidate's profile. Otherwise false.\n"
    "</human_review_flag>\n\n"
    "<output_format>\n"
    'Respond with ONLY a JSON object of the form: {"selected": [{"id": "<id>", '
    '"text": "<final bullet text>"}], "human_review": <true|false>}. No prose.\n'
    "</output_format>"
)

# Markdown [text](url) — the link syntax RenderCV turns into a clickable PDF link.
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")

# Spans keyword-bolding must not touch: existing **bold** and [text](url) links.
_PROTECTED_SPAN_RE = re.compile(r"\*\*.+?\*\*|\[[^\]]*\]\([^)\s]*\)")


def markdown_link_urls(text: str) -> set[str]:
    """The set of URLs carried by Markdown links in ``text``."""
    return set(_MD_LINK_RE.findall(text or ""))


def bold_keywords(text: str, keywords: list[str]) -> str:
    """Wrap JD-keyword occurrences in ``text`` in Markdown ``**bold**``.

    Presentation only — the words themselves never change, so grounding is
    unaffected (asterisks are not content tokens). Matching is case-insensitive
    but keeps the bullet's original casing; longest keyword wins ("machine
    learning" beats "learning"); existing ``**bold**`` spans and Markdown links
    are left untouched, and the lookarounds never produce ``***`` runs.

    Runs as a deterministic post-pass in BOTH tailoring paths, so keyword bolding
    is guaranteed even when the LLM ignores its formatting instruction.
    """
    kws = sorted({k.strip() for k in keywords if k and k.strip()}, key=len, reverse=True)
    if not text or not kws:
        return text
    pattern = re.compile(
        r"(?<![\w*])(" + "|".join(re.escape(k) for k in kws) + r")(?![\w*])",
        re.IGNORECASE,
    )

    def _bold(segment: str) -> str:
        return pattern.sub(r"**\1**", segment)

    out: list[str] = []
    last = 0
    for m in _PROTECTED_SPAN_RE.finditer(text):
        out.append(_bold(text[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_bold(text[last:]))
    return "".join(out)


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
    """System content blocks: stable instructions + full-résumé context (cached)."""
    return resume_system_blocks(
        SYSTEM_INSTRUCTIONS, resume, label="Reference context (do not fabricate beyond it)"
    )


def build_user_text(jd_text: str, keywords: list[str], candidate_bullets: list[BulletRef], limit: int) -> str:
    # Long context first, the actual ask last (long-context prompting guidance).
    lines = [
        "JOB DESCRIPTION:",
        jd_text,
        "",
        "EXTRACTED KEYWORDS: " + ", ".join(keywords),
        "",
        "CANDIDATE BULLETS:",
    ]
    for ref in candidate_bullets:
        lines.append(f"- [{ref.id}] {ref.text}")
    lines += [
        "",
        f"Select at most {limit} bullets by id, tailored to this job per your "
        "instructions, and respond with the JSON object only.",
    ]
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

    def _verbatim(refs: list[BulletRef]) -> list[TailoredBullet]:
        # Words verbatim; keyword bolding is a presentation-only post-pass.
        return [TailoredBullet(ref=r, text=bold_keywords(r.text, keywords)) for r in refs]

    if complete is None:
        # Deterministic select-only: keep the (already fit-ordered) bullets verbatim.
        return TailorResult(
            bullets=_verbatim(candidate_bullets[:max_bullets]),
            human_review=human_review,
            used_llm=False,
        )

    system_blocks = build_system_blocks(resume)
    user_text = build_user_text(jd_text, keywords, candidate_bullets, max_bullets)
    data = complete(system_blocks, user_text)

    selected = data.get("selected") if isinstance(data, dict) else None
    if not isinstance(selected, list):
        log.warning("LLM returned no usable selection; falling back to verbatim top bullets")
        return TailorResult(
            bullets=_verbatim(candidate_bullets[:max_bullets]),
            human_review=human_review,
            used_llm=True,
        )

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
        out.append(TailoredBullet(ref=ref, text=bold_keywords(text, keywords)))
        if len(out) >= max_bullets:
            break

    if not out:  # nothing usable came back — keep the résumé non-empty
        out = _verbatim(candidate_bullets[:max_bullets])

    llm_flag = bool(data.get("human_review")) if isinstance(data, dict) else False
    return TailorResult(bullets=out, human_review=human_review or llm_flag, used_llm=True)

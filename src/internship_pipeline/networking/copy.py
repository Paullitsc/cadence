"""Networking copy: the LinkedIn connect note and the post-accept message.

Same anti-hallucination philosophy as Phase-3 outreach copy, but there is no job
posting here — the ONLY company facts available are the target's own blurb from
the targets file. The deterministic templates (no API key) use nothing but real
fields; the LLM path is grounded-checked token-by-token against the blurb + the
candidate's real bullets, and any failing field falls back to the deterministic
version. Drafts only — the human sends everything on LinkedIn by hand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..logging_config import get_logger
from ..outreach.copy import _BOILERPLATE_VOCAB, LINKEDIN_NOTE_LIMIT, is_grounded
from ..resume.llm import CompleteFn, resume_system_blocks
from ..resume.matching import content_tokens
from ..resume.models import BulletRef, MasterResume
from .models import Person

log = get_logger(__name__)

SYSTEM_INSTRUCTIONS = (
    "<role>\n"
    "You write LinkedIn networking outreach for an internship candidate building "
    "relationships at startups: one connection-request note and one short message "
    "to send after the connection is accepted. You receive the target company (with "
    "its own description), the recipient (when known), and the candidate's REAL "
    "résumé bullets. A human reviews and sends everything by hand.\n"
    "</role>\n\n"
    "<strict_rules>\n"
    "These override anything else:\n"
    "1. Use ONLY facts supported by the provided bullets/profile. NEVER invent or "
    "alter experience, metrics, numbers, employers, schools, technologies, or "
    "skills. Do not claim to have used a technology the bullets don't mention.\n"
    "2. Use ONLY the provided company description for company facts. Do not import "
    "outside knowledge about the company, its products, or its people.\n"
    "3. This is relationship-building, not a job application: the candidate hopes "
    "to work at a startup like this one next year. Be honest about that; never "
    "invent an open role.\n"
    "</strict_rules>\n\n"
    "<craft>\n"
    "- Connect note: UNDER 280 characters. One specific hook from the company "
    "description, one real credential, a warm ask to connect. No links.\n"
    "- Message (post-accept): 50-100 words, 3-6 short sentences. Thank them for "
    "connecting, map 1-2 real bullets onto what the company does (from its "
    "description only), and close with ONE low-friction ask — a short call, or "
    "whether they take interns next summer.\n"
    "- Tone: warm, direct, peer-to-peer. No flattery, no hype, no clichés "
    "('passionate', 'huge fan', 'esteemed company').\n"
    "- Uniqueness test: if the text would still make sense sent to a different "
    "company, rewrite it around this company's description.\n"
    "</craft>\n\n"
    "<output_format>\n"
    'Respond with ONLY a JSON object: {"connect_note": "<note>", "message": '
    '"<post-accept message>"}. No prose.\n'
    "</output_format>"
)

# Words normal networking messages need beyond the shared cold-email vocabulary.
_NETWORKING_VOCAB: frozenset[str] = frozenset(
    """
    connecting connected connection accepting accepted request thanks thanked
    following followed follow startup startups founder founders early stage
    building builds internship internships interning intern interns summer
    joining join year next years hoping hope path advice journey grow growing
    growth story keep track touch reach network networking
    """.split()
)


@dataclass
class NetworkingContent:
    """One drafted artifact (the stage stores it on the ``Person`` row)."""

    body: str
    used_llm: bool = False


def _first_name(full: str | None) -> str | None:
    parts = (full or "").strip().split()
    return parts[0] if parts else None


def _intro(resume: MasterResume) -> str:
    return (resume.summary or "a computer science student").strip().rstrip(".")


def _variant(person: Person, count: int) -> int:
    """Stable 0..count-1 index from the person id (idempotent per person)."""
    digest = hashlib.sha256(person.person_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % count


def rank_bullets(resume: MasterResume, bullets: list[BulletRef], person: Person, limit: int = 3) -> list[BulletRef]:
    """The candidate's bullets most relevant to this company, best first.

    There is no JD to embed here — the signal is the company blurb — so a cheap
    deterministic token-overlap score is enough (and keeps the stage offline).
    Ties keep the master résumé's own order.
    """
    target = content_tokens(
        " ".join(filter(None, [person.company_name, person.company_blurb, person.role]))
    )
    scored = [
        (len(content_tokens(ref.searchable_text()) & target), i, ref)
        for i, ref in enumerate(bullets)
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [ref for _, _, ref in scored[:limit]]


def networking_vocab(person: Person, resume: MasterResume, top_bullets: list[BulletRef]) -> set[str]:
    """Every content token an LLM draft is allowed to contain."""
    vocab: set[str] = set(_BOILERPLATE_VOCAB) | set(_NETWORKING_VOCAB)
    vocab |= content_tokens(person.company_name)
    vocab |= content_tokens(person.company_blurb)
    if person.name:
        vocab |= content_tokens(person.name)
    if person.role:
        vocab |= content_tokens(person.role)
    for ref in top_bullets:
        vocab |= content_tokens(ref.searchable_text())
        vocab |= content_tokens(ref.parent)
    vocab |= content_tokens(resume.name or "")
    vocab |= content_tokens(resume.summary or "")
    for skill in resume.skills.all():
        vocab |= content_tokens(skill)
    for exp in resume.experiences:
        vocab |= content_tokens(exp.company)
        vocab |= content_tokens(exp.role)
    for proj in resume.projects:
        vocab |= content_tokens(proj.name)
    return vocab


# --------------------------------------------------------------------------- #
# Deterministic templates (always real, always grounded)
# --------------------------------------------------------------------------- #
def deterministic_connect_note(person: Person, resume: MasterResume) -> str:
    greeting = _first_name(person.name)
    lead = f"Hi {greeting} — " if greeting else "Hi — "
    name = _first_name(resume.name) or resume.name or "a candidate"
    intro = _intro(resume)
    company = person.company_name
    variant = _variant(person, 3)

    notes = [
        (
            f"{lead}I'm {name}, {intro}. I've been following {company} and I'm hoping "
            f"to work at a startup like it next year — would love to connect and "
            f"follow your work."
        ),
        (
            f"{lead}I'm {name}, {intro}. {company}'s work really stands out to me, and "
            f"I'm hoping to join a startup like it next year. Would love to connect!"
        ),
        (
            f"{lead}I'm {name}, {intro}. I'm building toward working at a startup like "
            f"{company} next year and would love to connect and learn from your work."
        ),
    ]
    note = notes[variant]
    if len(note) > LINKEDIN_NOTE_LIMIT:
        note = note[: LINKEDIN_NOTE_LIMIT - 1].rstrip() + "…"
    return note


def deterministic_message(person: Person, resume: MasterResume, top_bullets: list[BulletRef]) -> str:
    greeting = _first_name(person.name) or "there"
    name = resume.name or "the candidate"
    intro = _intro(resume)
    company = person.company_name
    variant = _variant(person, 3)

    openings = [
        f"Thanks for connecting! I'm {name}, {intro}.",
        f"Thanks for accepting — I'm {name}, {intro}.",
        f"Great to be connected! I'm {name}, {intro}.",
    ]
    bullet_intros = [
        f"A couple of things I've built that feel relevant to {company}:",
        "Some recent work of mine that maps to what you're building:",
        f"A few projects that feel close to {company}'s work:",
    ]
    asks = [
        (
            f"I'm hoping to work at a startup like {company} next year — if the team "
            f"ever takes interns, I'd love to hear how to put my name in early. "
            f"Either way, glad to be connected!"
        ),
        (
            f"I'd love 15 minutes to hear about your work at {company} sometime — and "
            f"if the team takes interns next summer, I'd love to know how to apply "
            f"early. Either way, glad to be connected!"
        ),
        (
            f"If {company} takes interns next summer I'd love to put my name in early "
            f"— and I'm happy to share more of my background any time. Glad to be "
            f"connected either way!"
        ),
    ]

    body = [f"Hi {greeting},", "", openings[variant]]
    lines = [
        # Plain-text channel — strip résumé Markdown bold.
        f"- {ref.text.replace('**', '')} ({ref.parent})"
        for ref in top_bullets[:2]
    ]
    if lines:
        body += ["", bullet_intros[variant], *lines]
    body += ["", asks[variant], "", "Best,", name]
    return "\n".join(body)


def _build_user_text(person: Person, top_bullets: list[BulletRef], resume: MasterResume) -> str:
    lines = [
        f"CANDIDATE: {resume.name}",
        f"TARGET COMPANY: {person.company_name}",
    ]
    if person.company_blurb:
        lines.append(f"COMPANY DESCRIPTION (the only company facts): {person.company_blurb}")
    if person.name:
        lines.append(f"RECIPIENT: {person.name}" + (f" ({person.role})" if person.role else ""))
    lines += ["", "CANDIDATE'S MOST RELEVANT REAL BULLETS (reference only these):"]
    lines += [f"- {ref.text.replace('**', '')} [{ref.parent}]" for ref in top_bullets]
    return "\n".join(lines)


def draft_networking_copy(
    *,
    person: Person,
    resume: MasterResume,
    top_bullets: list[BulletRef],
    complete: CompleteFn | None = None,
) -> tuple[NetworkingContent, NetworkingContent]:
    """Draft ``(connect_note, message)`` for one person, grounded in real data.

    One call produces both artifacts (they share all their context; the stage
    stores whichever one the person's status needs). With ``complete=None`` the
    deterministic templates are returned; with an LLM each field must pass the
    grounding check or it falls back to its deterministic version.
    """
    det_note = deterministic_connect_note(person, resume)
    det_message = deterministic_message(person, resume, top_bullets)

    if complete is None:
        return NetworkingContent(det_note), NetworkingContent(det_message)

    vocab = networking_vocab(person, resume, top_bullets)
    try:
        data = complete(
            resume_system_blocks(
                SYSTEM_INSTRUCTIONS, resume, label="Reference context (do not fabricate beyond it)"
            ),
            _build_user_text(person, top_bullets, resume),
        )
    except Exception as exc:  # LLM error must not break the run — use the safe template
        log.warning("networking LLM call failed; using deterministic copy", extra={"error": repr(exc)})
        return NetworkingContent(det_note), NetworkingContent(det_message)

    def _grounded_or(field: str, fallback: str, limit: int | None = None) -> tuple[str, bool]:
        val = data.get(field) if isinstance(data, dict) else None
        if isinstance(val, str) and val.strip() and is_grounded(val, vocab):
            val = val.strip()
            if limit is None or len(val) <= limit:
                return val, True
        return fallback, False

    note, note_llm = _grounded_or("connect_note", det_note, limit=LINKEDIN_NOTE_LIMIT)
    message, message_llm = _grounded_or("message", det_message)
    return NetworkingContent(note, used_llm=note_llm), NetworkingContent(message, used_llm=message_llm)

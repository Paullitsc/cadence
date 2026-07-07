"""Outreach copy: a short, specific cold email + a LinkedIn note (Phase 3).

Reuses the Phase-2 retrieval output (the top-K real résumé bullets already scored
for this job) so the draft references the company and the 1-2 most relevant REAL
projects — never invented ones. Same dependency-injected ``CompleteFn`` pattern as
Phase-2 tailoring/answers: the live path uses Claude with a strict real-data-only
system prompt; with no API key it produces a deterministic template from real fields.

Anti-hallucination guardrail (same philosophy as résumé tailoring): after the LLM
responds, each field is checked so it introduces no content token outside the
tailoring INPUT (job text + keywords + the candidate's real bullets/skills/identity
+ the recipient's name + a fixed set of ordinary email words). Any field that fails
falls back to the deterministic, fully-grounded template. Fabricated metrics,
employers, or projects therefore cannot reach a drafted message.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_config import get_logger
from ..models import Job
from ..resume.llm import CompleteFn
from ..resume.matching import content_tokens
from ..resume.models import BulletRef, MasterResume
from .contacts import Contact

log = get_logger(__name__)

LINKEDIN_NOTE_LIMIT = 300  # LinkedIn connection-request notes are capped at 300 chars

SYSTEM_INSTRUCTIONS = (
    "You draft cold outreach for a job-seeking candidate: one short email and one "
    "LinkedIn connection note. You receive the job, the target company, and the "
    "candidate's REAL résumé bullets.\n\n"
    "STRICT RULES — these override anything else:\n"
    "1. Use ONLY facts supported by the provided bullets/profile. NEVER invent or "
    "alter experience, metrics, numbers, employers, schools, technologies, or skills.\n"
    "2. Reference the company by name and the 1-2 most relevant real projects/bullets.\n"
    "3. Be specific and concise: the email is 4-7 sentences; the LinkedIn note is under "
    "300 characters. Warm, human, no clichés, no hype, no fabricated flattery.\n"
    "4. Do not promise anything untrue. Do not claim to have used a technology the "
    "bullets don't mention.\n\n"
    'Respond with ONLY a JSON object: {"subject": "<email subject>", "email_body": '
    '"<email body, no signature block>", "linkedin_note": "<note>"}. No prose.'
)

# Ordinary cold-email vocabulary that is always allowed by the grounding check (so an
# honest LLM draft is not rejected for using normal English). Fabricated FACTS —
# metrics, unfamiliar company/school names, un-cited technologies — fall outside this
# set and the job/profile vocab, so they are what gets caught.
_BOILERPLATE_VOCAB: frozenset[str] = frozenset(
    """
    hi hello hey dear there im interested excited keen drawn admire admired impressed
    reaching reach out about role position internship intern opening opportunity team
    company work working build building built been recently currently my name is a an
    student studying computer science engineering software developer background profile
    relevant closely aligns align particularly especially notably really genuinely
    would love chance like to connect chat quick call learn more of the your our their
    happy share discuss talk hear best regards thanks thank you sincerely warm couple
    things worked feel that this it its as with for on in and or so lines line up close
    resume résumé portfolio github over few last summer new grad time application applied
    apply hoping look forward glad if open who what where when why how am also just here
    something part role's group org organization spent focused focus lately these those
    strong solid real world hands-on hands
    """.split()
)


@dataclass
class OutreachContent:
    subject: str
    email_body: str  # copy only — the CAN-SPAM footer is appended later, at persist time
    linkedin_note: str
    used_llm: bool = False
    human_review: bool = False


def _first_name(full: str | None) -> str | None:
    if not full:
        return None
    parts = full.strip().split()
    return parts[0] if parts else None


def _intro(resume: MasterResume) -> str:
    return (resume.summary or "a computer science student").strip().rstrip(".")


def allowed_vocab(
    job: Job,
    keywords: list[str],
    top_bullets: list[BulletRef],
    resume: MasterResume,
    contact: Contact,
) -> set[str]:
    """Every content token the outreach draft is allowed to contain."""
    vocab: set[str] = set(_BOILERPLATE_VOCAB)
    vocab |= content_tokens(job.title)
    vocab |= content_tokens(job.company_name)
    for loc in job.locations:
        vocab |= content_tokens(loc)
    if job.description:
        vocab |= content_tokens(job.description)
    for kw in keywords:
        vocab |= content_tokens(kw)
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
    if contact.name:
        vocab |= content_tokens(contact.name)
    if contact.title:
        vocab |= content_tokens(contact.title)
    return vocab


def is_grounded(text: str, vocab: set[str]) -> bool:
    """True if every content token in ``text`` is present in the allowed ``vocab``."""
    ungrounded = content_tokens(text) - vocab
    if ungrounded:
        log.warning("rejected ungrounded outreach text", extra={"ungrounded_tokens": sorted(ungrounded)})
        return False
    return True


# --------------------------------------------------------------------------- #
# Deterministic templates (always real, always grounded)
# --------------------------------------------------------------------------- #
def deterministic_subject(job: Job, resume: MasterResume) -> str:
    who = _first_name(resume.name) or "Introduction"
    return f"{who} — interested in the {job.title} role at {job.company_name}"


def _relevant_lines(top_bullets: list[BulletRef], limit: int) -> list[str]:
    lines = []
    for ref in top_bullets[:limit]:
        # Outreach is plain text — résumé Markdown bold would show as literal `**`.
        lines.append(f"- {ref.text.replace('**', '')} ({ref.parent})")
    return lines


def deterministic_email(
    job: Job, resume: MasterResume, top_bullets: list[BulletRef], contact: Contact, max_projects: int
) -> str:
    greeting = _first_name(contact.name) or "there"
    name = resume.name or "the candidate"
    body = [
        f"Hi {greeting},",
        "",
        f"I'm {name}, {_intro(resume)}. I'm reaching out about the {job.title} role at "
        f"{job.company_name} — it lines up closely with what I've been building.",
    ]
    lines = _relevant_lines(top_bullets, max_projects)
    if lines:
        body += ["", "A couple of things I've worked on that feel relevant:", *lines]
    body += [
        "",
        "I'd love the chance to connect and learn more about the team. Happy to share "
        "more of my background or a tailored resume.",
        "",
        "Best,",
        name,
    ]
    return "\n".join(body)


def deterministic_linkedin(job: Job, resume: MasterResume, top_bullets: list[BulletRef], contact: Contact) -> str:
    greeting = _first_name(contact.name)
    name = _first_name(resume.name) or resume.name or "a candidate"
    lead = f"Hi {greeting} — " if greeting else "Hi — "
    note = (
        f"{lead}I'm {name}, {_intro(resume)}. I really admire {job.company_name}'s work "
        f"and I'm interested in the {job.title} role. Would love to connect!"
    )
    if len(note) > LINKEDIN_NOTE_LIMIT:
        note = note[: LINKEDIN_NOTE_LIMIT - 1].rstrip() + "…"
    return note


def build_user_text(job: Job, keywords: list[str], top_bullets: list[BulletRef], contact: Contact, resume: MasterResume) -> str:
    lines = [
        f"CANDIDATE: {resume.name}",
        f"JOB: {job.title} at {job.company_name}",
    ]
    if job.locations:
        lines.append("LOCATION: " + ", ".join(job.locations))
    if keywords:
        lines.append("JOB KEYWORDS: " + ", ".join(keywords))
    if contact.name:
        lines.append(f"RECIPIENT: {contact.name}" + (f" ({contact.title})" if contact.title else ""))
    lines += ["", "CANDIDATE'S MOST RELEVANT REAL BULLETS (reference only these):"]
    # Plain-text channel: strip résumé Markdown bold so the LLM never echoes `**`.
    lines += [f"- {ref.text.replace('**', '')} [{ref.parent}]" for ref in top_bullets]
    return "\n".join(lines)


def build_system_blocks(resume: MasterResume) -> list[dict]:
    """Stable instructions + cached candidate context (same pattern as Phase 2)."""
    ctx = [f"CANDIDATE: {resume.name}"]
    if resume.summary:
        ctx.append(f"SUMMARY: {resume.summary}")
    if resume.skills.all():
        ctx.append("SKILLS: " + ", ".join(resume.skills.all()))
    return [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"Reference context (do not fabricate beyond it):\n{chr(10).join(ctx)}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def draft_outreach_copy(
    *,
    job: Job,
    contact: Contact,
    keywords: list[str],
    top_bullets: list[BulletRef],
    resume: MasterResume,
    complete: CompleteFn | None = None,
    human_review: bool = False,
    max_projects: int = 2,
) -> OutreachContent:
    """Draft the email + LinkedIn note for one job, grounded against real data.

    With ``complete=None`` (no LLM) returns the deterministic template. With an LLM,
    each returned field must pass the grounding check or it falls back to the
    deterministic version of that field — so no field can carry a fabricated fact.
    """
    det_subject = deterministic_subject(job, resume)
    det_email = deterministic_email(job, resume, top_bullets, contact, max_projects)
    det_linkedin = deterministic_linkedin(job, resume, top_bullets, contact)

    if complete is None:
        return OutreachContent(det_subject, det_email, det_linkedin, used_llm=False, human_review=human_review)

    vocab = allowed_vocab(job, keywords, top_bullets, resume, contact)
    try:
        data = complete(build_system_blocks(resume), build_user_text(job, keywords, top_bullets, contact, resume))
    except Exception as exc:  # LLM error must not break the run — use the safe template
        log.warning("outreach LLM call failed; using deterministic copy", extra={"error": repr(exc)})
        return OutreachContent(det_subject, det_email, det_linkedin, used_llm=False, human_review=human_review)

    def _grounded_or(field: str, fallback: str) -> str:
        val = data.get(field) if isinstance(data, dict) else None
        if isinstance(val, str) and val.strip() and is_grounded(val, vocab):
            return val.strip()
        return fallback

    subject = _grounded_or("subject", det_subject)
    email_body = _grounded_or("email_body", det_email)
    linkedin_note = _grounded_or("linkedin_note", det_linkedin)
    if len(linkedin_note) > LINKEDIN_NOTE_LIMIT:  # keep within LinkedIn's limit regardless
        linkedin_note = det_linkedin
    return OutreachContent(subject, email_body, linkedin_note, used_llm=True, human_review=human_review)

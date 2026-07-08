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

import hashlib
from dataclasses import dataclass

from ..logging_config import get_logger
from ..models import Job
from ..resume.llm import CompleteFn, resume_system_blocks
from ..resume.matching import content_tokens
from ..resume.models import BulletRef, MasterResume
from .contacts import Contact

log = get_logger(__name__)

LINKEDIN_NOTE_LIMIT = 300  # LinkedIn connection-request notes are capped at 300 chars

SYSTEM_INSTRUCTIONS = (
    "<role>\n"
    "You write cold outreach for an internship candidate: one short email and one "
    "LinkedIn connection note. You receive the job posting, the target company, the "
    "recipient (when known), and the candidate's REAL résumé bullets. A human "
    "reviews and sends everything.\n"
    "</role>\n\n"
    "<strict_rules>\n"
    "These override anything else:\n"
    "1. Use ONLY facts supported by the provided bullets/profile. NEVER invent or "
    "alter experience, metrics, numbers, employers, schools, technologies, or "
    "skills. Do not claim to have used a technology the bullets don't mention. Do "
    "not promise anything untrue.\n"
    "2. Anchor your wording in the job posting's own language and the candidate's "
    "bullets. Do not import outside knowledge about the company — use only what "
    "the posting itself says.\n"
    "3. Reference the company by name and the 1-2 most relevant real bullets.\n"
    "</strict_rules>\n\n"
    "<craft>\n"
    "- Email: 60-120 words, 4-7 short sentences. Short beats clever.\n"
    "- Subject: at most 8 words, naming the company or the role's specifics. Never "
    "generic ('Internship Inquiry', 'Opportunity', 'Hello').\n"
    "- Open with a specific hook: the actual product area, technology, or problem "
    "this team works on, taken from the posting. Never open with 'I hope this "
    "email finds you well', 'My name is', or a restatement of the job title alone.\n"
    "- Then map 1-2 real bullets onto what the role needs, and make the mapping "
    "explicit: the team is doing X; the candidate built Y.\n"
    "- Close with ONE low-friction ask — a short call or one specific question "
    "about the team. Never 'please consider my application'.\n"
    "- Uniqueness test: if the email would still make sense sent to a different "
    "company, rewrite it. The hook and the mapping must only fit THIS posting.\n"
    "- Tone: warm, direct, peer-to-peer. No flattery, no hype, no clichés "
    "('passionate', 'perfect fit', 'esteemed company').\n"
    "- LinkedIn note: under 300 characters — one specific hook, one real "
    "credential, an ask to connect.\n"
    "</craft>\n\n"
    "<email_shape>\n"
    "Hi [first name],\n"
    "[Hook: the specific thing this team builds or solves, in the posting's own "
    "words.]\n"
    "[Mapping: the posting asks for X — I built Y, which did Z (a real bullet).]\n"
    "[Ask: one specific question, or a short call.]\n"
    "</email_shape>\n\n"
    "<output_format>\n"
    'Respond with ONLY a JSON object: {"subject": "<email subject>", "email_body": '
    '"<email body, no signature block>", "linkedin_note": "<note>"}. No prose.\n'
    "</output_format>"
)

# Ordinary cold-email vocabulary that is always allowed by the grounding check (so an
# honest LLM draft is not rejected for using normal English). Fabricated FACTS —
# metrics, unfamiliar company/school names, un-cited technologies — fall outside this
# set and the job/profile vocab, so they are what gets caught. Deliberately contains
# NO numbers, no tech terms, and no proper-noun-like words.
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
    ve re ll don isn aren haven wasn weren couldn wouldn shouldn doesn hasn hadn didn won
    noticed see seeing saw read reading came spotted caught eye stood posting posted
    listing listed page careers site mission product products platform space problem
    problems solve solving solves approach challenge challenges match matches matching
    similar overlap overlaps maps mirrors directly exactly question questions ask asking
    minutes minute grab coffee schedule week available availability free university
    college major majoring semester junior senior sophomore freshman appreciate
    appreciated either way anyway cheers much take taking bit lot sense make makes
    making get getting hearing moment briefly brief short curious wondering wonder
    enjoy enjoyed find found based note message
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


def _template_variant(job: Job, count: int) -> int:
    """Stable 0..count-1 index from the job's dedupe key (idempotent per job)."""
    digest = hashlib.sha256(job.dedupe_key().encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % count


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
    intro = _intro(resume)
    variant = _template_variant(job, 3)

    openings = [
        (
            f"I'm {name}, {intro}. I'm reaching out about the {job.title} role at "
            f"{job.company_name} — it lines up closely with what I've been building."
        ),
        (
            f"I'm {name}, {intro}. The {job.title} opening at {job.company_name} caught "
            f"my eye — the work lines up with what I've been building lately."
        ),
        (
            f"I'm {name}, {intro}. I noticed the {job.title} role at {job.company_name} "
            f"and wanted to reach out — it maps closely to my recent work."
        ),
    ]
    project_intros = [
        "A couple of things I've worked on that feel relevant:",
        "A few projects that map to what your team is building:",
        "Some work I've done that feels closely related:",
    ]
    asks = [
        (
            "I'd love the chance to connect and learn more about the team. Happy to share "
            "more of my background or a tailored resume."
        ),
        (
            "Would you be open to a quick chat about the team? Happy to share more of my "
            "background or a tailored resume."
        ),
        (
            "If you have a few minutes, I'd love to hear more about the team. Happy to "
            "share my background or a tailored resume."
        ),
    ]

    body = [f"Hi {greeting},", "", openings[variant]]
    lines = _relevant_lines(top_bullets, max_projects)
    if lines:
        body += ["", project_intros[variant], *lines]
    body += ["", asks[variant], "", "Best,", name]
    return "\n".join(body)


def deterministic_linkedin(job: Job, resume: MasterResume, top_bullets: list[BulletRef], contact: Contact) -> str:
    greeting = _first_name(contact.name)
    name = _first_name(resume.name) or resume.name or "a candidate"
    intro = _intro(resume)
    lead = f"Hi {greeting} — " if greeting else "Hi — "
    variant = _template_variant(job, 3)

    notes = [
        (
            f"{lead}I'm {name}, {intro}. I really admire {job.company_name}'s work "
            f"and I'm interested in the {job.title} role. Would love to connect!"
        ),
        (
            f"{lead}I'm {name}, {intro}. The {job.title} role at {job.company_name} "
            f"lines up with my background — would love to connect!"
        ),
        (
            f"{lead}I'm {name}, {intro}. I noticed the {job.title} opening at "
            f"{job.company_name} and would love to connect!"
        ),
    ]
    note = notes[variant]
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
    return resume_system_blocks(
        SYSTEM_INSTRUCTIONS, resume, label="Reference context (do not fabricate beyond it)"
    )


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

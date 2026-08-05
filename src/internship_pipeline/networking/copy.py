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
    "its own description and, when the candidate has researched one, a SPECIFIC "
    "hook), the recipient and their background (when known), and the candidate's "
    "REAL résumé bullets. A human reviews and sends everything by hand.\n"
    "</role>\n\n"
    "<strict_rules>\n"
    "These override anything else:\n"
    "1. Use ONLY facts supported by the provided bullets/profile. NEVER invent or "
    "alter experience, metrics, numbers, employers, schools, technologies, or "
    "skills. Do not claim to have used a technology the bullets don't mention.\n"
    "2. Use ONLY the provided company description and company hook for company "
    "facts, and ONLY the provided recipient background for facts about the "
    "recipient. Do not import outside knowledge about the company, its products, "
    "or its people — not even things you happen to know are true.\n"
    "3. If a field is marked (not provided), SKIP that beat entirely. Never guess "
    "at what a company builds or what a person has worked on.\n"
    "4. This is relationship-building, not a job application. Never invent an open "
    "role.\n"
    "5. React to the hook using ITS OWN words. Do not introduce technical nouns "
    "that appear nowhere in the material you were given — an automated check "
    "rejects the whole draft on a single unsupported term, and the candidate then "
    "sends a generic template instead. Stance and curiosity are free ('I'd assumed', "
    "'I was surprised'); new domain vocabulary is not.\n"
    "</strict_rules>\n\n"
    "<message_shape>\n"
    "The post-accept message is the important one. It is a peer-to-peer note to a "
    "person the candidate finds genuinely interesting — NOT an application. Write "
    "90-140 words as 4 short paragraphs separated by blank lines:\n"
    "1. One line: greet them by first name and thank them for connecting.\n"
    "2. One sentence introducing the candidate — year, program, school, and what "
    "they are currently into. Compact; no list of achievements.\n"
    "3. THE SPECIFIC BEAT, and the reason the message gets a reply. Name the "
    "company hook and give an honest reaction to it, then turn to THEM: name their "
    "background and ask a real question about it — how that experience shapes or "
    "translates to what they do now. If only one of hook/background is provided, "
    "use that one and go deeper on it.\n"
    "4. The ask: 15 minutes for a call, at their convenience, framed as wanting to "
    "hear about their work. Then a sign-off.\n"
    "</message_shape>\n\n"
    "<never_in_the_message>\n"
    "- NO bullet points or lists. The message is flowing prose.\n"
    "- NO résumé dump. At most ONE compact clause about the candidate's own work, "
    "and only if it directly connects to what you just said about them.\n"
    "- NO asking for an internship, and NO 'if the team takes interns I'd love to "
    "put my name in early'. The ask is a conversation, nothing more. The candidate "
    "is looking, and the recipient knows it — saying so cheapens the note.\n"
    "- NO flattery, hype, or clichés ('passionate', 'huge fan', 'excited about "
    "your journey', 'impressive work').\n"
    "- NO sentence that would still make sense sent to a different company or a "
    "different person. Every sentence in beat 3 must fail that test.\n"
    "</never_in_the_message>\n\n"
    "<connect_note>\n"
    "UNDER 280 characters, since LinkedIn truncates. One specific hook (the "
    "company hook if provided, otherwise the description), a half-line of who the "
    "candidate is, and a warm ask to connect. No links, no lists, no internship "
    "ask.\n"
    "</connect_note>\n\n"
    "<tone_example>\n"
    "Shape and register ONLY — every fact in it is fictional and off-limits:\n"
    '"Hi Dana, thanks for the connect!\\n\\nI\'m Alex Chen, a 2nd-year '
    "Computer Engineering student at Example University who has been going deep on "
    "distributed systems lately.\\n\\nI read up on Widgetly's incremental "
    "reindexing and was genuinely surprised it holds under that write volume — I'd "
    "assumed you'd have to batch it. I also noticed your background in query "
    "planning, and I'm curious how much of that intuition carried over to the "
    "problems you're working on now.\\n\\nWould you have 15 minutes in the next "
    "week or two, whenever suits you, to talk about it?\\n\\nThanks for your "
    'time,\\nAlex Chen"\n'
    "</tone_example>\n\n"
    "<output_format>\n"
    'Respond with ONLY a JSON object: {"connect_note": "<note>", "message": '
    '"<post-accept message>"}. No prose.\n'
    "</output_format>"
)

# Words normal networking messages need beyond the shared cold-email vocabulary.
#
# The grounding check (``is_grounded``) rejects a whole field if ONE content token
# is missing here, so a too-narrow list doesn't produce a safer draft — it silently
# throws the LLM draft away and ships the deterministic template instead. The line
# these lists draw is therefore: a word belongs here if it cannot, on its own,
# constitute a fabricated FACT. Stance, curiosity and connective tissue are safe;
# technologies, employers, metrics and product names are not and must keep coming
# from the blurb/hook/bullets.
_NETWORKING_VOCAB: frozenset[str] = frozenset(
    """
    connecting connected connection accepting accepted request thanks thanked
    following followed follow startup startups founder founders early stage
    building builds internship internships interning intern interns summer
    joining join year next years hoping hope path advice journey grow growing
    growth story keep track touch reach network networking
    """.split()
)

# The personal, curiosity-led register the message rewrite asks for. Without these
# a draft in the intended voice ("I'm curious how that expertise translates…")
# fails grounding on ordinary English and falls back to the template.
_CURIOSITY_VOCAB: frozenset[str] = frozenset(
    """
    fascinated fascinating intrigued intriguing compelling interesting impressive
    dug digging dove dive dived explored exploring exploration reading recent
    translate translates translated carries carry carrying shapes shape shaping
    expertise specialty specialized specializing experience experiences angle
    aspect aspects core depth deeper deep adjacent overlap trajectory transition
    tackle tackling tackled navigating handling wrestling thinking thought
    understand understanding learn learning perspective view views take
    conversation convenience anytime sometime whenever calendar chance
    today current genuine honestly truly particular specifically
    side end front area field domain kind sort keep coming back stuck
    especially interested love hear hearing wanted wondered
    was were been being going gone whole entire
    assumed assume expected expect guessed figured imagined thought
    seemed seems sounds sounded looks looked felt feels struck surprised
    hard easy simple straightforward obvious tricky subtle messy harder
    """.split()
)

# The one deliberate NUMERIC exception. Numbers are otherwise exactly what the
# grounding check exists to catch (fabricated metrics), but a call ask reads far
# better with a concrete duration — and "15 minutes" is the ask in every
# deterministic template, so without this the LLM path is rejected for writing
# the same sentence the fallback writes. Durations only; a fabricated metric
# would still need its surrounding claim words, which must come from real bullets.
_DURATION_VOCAB: frozenset[str] = frozenset("10 15 20 25 30 45".split())


EMAIL_SYSTEM_INSTRUCTIONS = (
    "<role>\n"
    "You write a short cold ESCALATION EMAIL for an internship candidate building "
    "relationships at startups. The candidate already tried to reach this person on "
    "LinkedIn (a connection request or message) and it went quiet, so this email is a "
    "polite direct follow-up. You receive the target company (with its own "
    "description), the recipient (when known), and the candidate's REAL résumé "
    "bullets. A human reviews and sends everything by hand.\n"
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
    "invent an open role. Do NOT invent a physical address, phone number, or "
    "unsubscribe line — a compliant footer is appended automatically.\n"
    "</strict_rules>\n\n"
    "<craft>\n"
    "- Subject: under 70 characters, specific, no clickbait. Reference the company.\n"
    "- Body: 60-110 words, 4-6 short sentences. Acknowledge the earlier LinkedIn "
    "outreach briefly, introduce the candidate in one line, map 1-2 real bullets "
    "onto what the company does (from its description only), and close with ONE "
    "low-friction ask — a short call, or whether they take interns next summer.\n"
    "- Tone: warm, direct, peer-to-peer. No flattery, no hype, no clichés "
    "('passionate', 'huge fan', 'esteemed company'). Do not be pushy about the "
    "unanswered LinkedIn attempt.\n"
    "- Uniqueness test: if the text would still make sense sent to a different "
    "company, rewrite it around this company's description.\n"
    "</craft>\n\n"
    "<output_format>\n"
    'Respond with ONLY a JSON object: {"subject": "<subject>", "body": "<email '
    'body, no signature block beyond a simple sign-off>"}. No prose.\n'
    "</output_format>"
)

# Email-specific words allowed on top of the shared + networking vocabulary (the
# grounding check only runs on LLM output; deterministic templates are exempt).
_EMAIL_VOCAB: frozenset[str] = frozenset(
    """
    email emailed reaching reached directly recently wanted followed following
    linkedin message messaged sent tried quiet response respond hearing heard
    quick minutes call chat time subject regards hello team inbox
    """.split()
)


@dataclass
class NetworkingContent:
    """One drafted artifact (the stage stores it on the ``Person`` row)."""

    body: str
    used_llm: bool = False


@dataclass
class NetworkingEmail:
    """A drafted escalation email (Phase 6b): subject + body, stored on the row."""

    subject: str
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

    The researched hook and the recipient's background are part of the query too:
    they are the most specific words available, so they pull up the bullet that
    actually resonates with THIS person rather than with the company's marketing.
    """
    target = content_tokens(
        " ".join(
            filter(
                None,
                [
                    person.company_name,
                    person.company_blurb,
                    person.company_hook,
                    person.role,
                    person.background,
                ],
            )
        )
    )
    scored = [
        (len(content_tokens(ref.searchable_text()) & target), i, ref)
        for i, ref in enumerate(bullets)
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [ref for _, _, ref in scored[:limit]]


def networking_vocab(person: Person, resume: MasterResume, top_bullets: list[BulletRef]) -> set[str]:
    """Every content token an LLM draft is allowed to contain."""
    vocab: set[str] = (
        set(_BOILERPLATE_VOCAB)
        | set(_NETWORKING_VOCAB)
        | set(_CURIOSITY_VOCAB)
        | set(_DURATION_VOCAB)
    )
    vocab |= content_tokens(person.company_name)
    vocab |= content_tokens(person.company_blurb)
    vocab |= content_tokens(person.company_hook)
    vocab |= content_tokens(person.background)
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

    # A researched hook makes the note concrete, but the cap is hard and hooks are
    # free text — so only take the hook variant when it genuinely fits. Truncating
    # mid-hook would read worse than the generic note.
    if person.company_hook:
        # The company is always named BEFORE the hook: hooks are noun phrases that
        # often start with a possessive ("their planner…"), which only resolves
        # once the reader knows who "they" are.
        hooked = [
            f"{lead}I'm {name}, {intro}. I've been reading about {company} — "
            f"{person.company_hook} is what got me interested. Would love to connect!",
            f"{lead}I'm {name}, {intro}. {company} caught my eye: "
            f"{person.company_hook}. Would love to connect and follow your work.",
            f"{lead}I'm {name}, {intro}. I keep coming back to {company}'s work — "
            f"{person.company_hook}. Would love to connect!",
        ]
        note = hooked[variant]
        if len(note) <= LINKEDIN_NOTE_LIMIT:
            return note

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
    """The post-accept message, built only from real fields.

    Mirrors the LLM's four beats (greet / who I am / the specific beat / ask a
    call) so the no-API-key path reads like the same person wrote it. The specific
    beat is only as good as the roster: with ``company_hook`` and ``background``
    filled in it names both; with neither it degrades to ONE credential in prose.
    It never dumps a bullet list and never asks for an internship — that ask is
    what made the old template read as a form letter.
    """
    greeting = _first_name(person.name) or "there"
    name = resume.name or "the candidate"
    intro = _intro(resume)
    company = person.company_name
    variant = _variant(person, 3)

    openings = [
        f"Hi {greeting}, thanks for the connect!",
        f"Hi {greeting}, thanks for connecting!",
        f"Hi {greeting} — glad we're connected!",
    ]
    body = [openings[variant], "", f"I'm {name}, {intro}."]

    if person.company_hook:
        hooks = [
            f"I've been reading up on {company}, and {person.company_hook} is the part "
            f"I keep coming back to.",
            f"I recently dug into what {company} is building — {person.company_hook} "
            f"especially caught my attention.",
            f"I've been following {company} for a while, and {person.company_hook} is "
            f"what really got me interested.",
        ]
        body += ["", hooks[variant]]

    if person.background:
        theirs = [
            f"I also noticed your background in {person.background} — I'm curious how "
            f"that shapes the problems you're working on now.",
            f"Your background in {person.background} stood out to me, and I'd be "
            f"curious how that experience translates to your work at {company} today.",
            f"I saw your background in {person.background}, and I'd love to understand "
            f"how much of that carries into what you're doing now.",
        ]
        body += ["", theirs[variant]]
    elif not person.company_hook and top_bullets:
        # Nothing researched about the company or the person yet: fall back to a
        # single credential, in prose. Weaker, but still not a résumé dump — and
        # the fix is to fill in the roster, which the digest nudges toward.
        ref = top_bullets[0]
        # Bullets usually end in a period; the parenthetical follows it.
        text = ref.text.replace("**", "").rstrip(".")
        relevant = [
            f"Recent work of mine that feels adjacent: {text} ({ref.parent}).",
            f"On my end, the closest thing I've built: {text} ({ref.parent}).",
            f"Something of mine that overlaps: {text} ({ref.parent}).",
        ]
        body += ["", relevant[variant]]

    asks = [
        f"Would you have 15 minutes in the next week or two, whenever suits you, for "
        f"a quick call about your work at {company}?",
        f"Any chance you'd have 15 minutes at your convenience for a quick call? I'd "
        f"really like to hear how you think about the work at {company}.",
        f"If you have 15 minutes sometime, I'd love a short call to hear more about "
        f"your work at {company} and where you see it going.",
    ]
    body += ["", asks[variant], "", "Thanks for your time,", name]
    return "\n".join(body)


_NOT_PROVIDED = "(not provided — skip this beat, do not guess)"


def _build_user_text(person: Person, top_bullets: list[BulletRef], resume: MasterResume) -> str:
    """The per-person turn.

    The two researched fields are always LABELLED, present or not: a silently
    omitted field invites the model to fill the gap from its own knowledge of the
    company, which rule 3 forbids. Stating "(not provided)" makes skipping the
    beat the obvious move.
    """
    lines = [
        f"CANDIDATE: {resume.name}",
        f"TARGET COMPANY: {person.company_name}",
    ]
    if person.company_blurb:
        lines.append(f"COMPANY DESCRIPTION (generic; company facts only): {person.company_blurb}")
    lines.append(
        "COMPANY HOOK (the specific thing to react to): "
        + (person.company_hook or _NOT_PROVIDED)
    )
    if person.name:
        lines.append(f"RECIPIENT: {person.name}" + (f" ({person.role})" if person.role else ""))
    lines.append(
        "RECIPIENT BACKGROUND (the specific thing to ask them about): "
        + (person.background or _NOT_PROVIDED)
    )
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


# --------------------------------------------------------------------------- #
# Phase 6b: the escalation email (subject + body)
# --------------------------------------------------------------------------- #
EMAIL_SUBJECT_LIMIT = 70


def deterministic_email(
    person: Person, resume: MasterResume, top_bullets: list[BulletRef]
) -> tuple[str, str]:
    """A real, grounded escalation email (subject, body) — no API key needed.

    Uses nothing but real fields; the stage appends the CAN-SPAM footer before it
    can be sent (same as Phase-3 outreach), so no footer is added here.
    """
    greeting = _first_name(person.name) or "there"
    name = resume.name or "the candidate"
    first = _first_name(resume.name) or name
    intro = _intro(resume)
    company = person.company_name
    variant = _variant(person, 3)

    subjects = [
        f"Following up from LinkedIn — {first} on {company}",
        f"{first} — reaching out about {company}",
        f"Quick note after LinkedIn — {company}",
    ]
    openings = [
        "I sent a connection request on LinkedIn recently and wanted to reach out "
        "directly in case it got buried.",
        "I reached out on LinkedIn a little while ago and thought I'd follow up here "
        "as well.",
        "I tried connecting on LinkedIn recently — reaching out directly in case that "
        "was easier to miss.",
    ]
    asks = [
        f"I'm hoping to work at a startup like {company} next year — if the team ever "
        f"takes interns, I'd love to hear how to put my name in early. Happy to share "
        f"more of my background any time.",
        f"I'd love 15 minutes to hear about your work at {company} — and if the team "
        f"takes interns next summer, I'd love to know how to apply early.",
        f"If {company} takes interns next summer I'd love to put my name in early, and "
        f"I'm glad to send along more of my work whenever useful.",
    ]

    subject = subjects[variant]
    if len(subject) > EMAIL_SUBJECT_LIMIT:
        subject = subject[: EMAIL_SUBJECT_LIMIT - 1].rstrip() + "…"

    body = [f"Hi {greeting},", "", openings[variant], "", f"I'm {name}, {intro}."]
    lines = [
        f"- {ref.text.replace('**', '')} ({ref.parent})" for ref in top_bullets[:2]
    ]
    if lines:
        body += ["", f"A couple of things I've built that feel relevant to {company}:", *lines]
    body += ["", asks[variant], "", "Best,", name]
    return subject, "\n".join(body)


def draft_networking_email(
    *,
    person: Person,
    resume: MasterResume,
    top_bullets: list[BulletRef],
    complete: CompleteFn | None = None,
) -> NetworkingEmail:
    """Draft the escalation email for one person, grounded in real data.

    With ``complete=None`` the deterministic template is returned; with an LLM both
    the subject and body must pass the grounding check (against the company blurb +
    real bullets) or that field falls back to its deterministic version.
    """
    det_subject, det_body = deterministic_email(person, resume, top_bullets)
    if complete is None:
        return NetworkingEmail(det_subject, det_body)

    vocab = networking_vocab(person, resume, top_bullets) | set(_EMAIL_VOCAB)
    try:
        data = complete(
            resume_system_blocks(
                EMAIL_SYSTEM_INSTRUCTIONS, resume,
                label="Reference context (do not fabricate beyond it)",
            ),
            _build_user_text(person, top_bullets, resume),
        )
    except Exception as exc:  # LLM error must not break the run — use the safe template
        log.warning("networking email LLM call failed; using deterministic copy", extra={"error": repr(exc)})
        return NetworkingEmail(det_subject, det_body)

    def _grounded_or(field: str, fallback: str, limit: int | None = None) -> tuple[str, bool]:
        val = data.get(field) if isinstance(data, dict) else None
        if isinstance(val, str) and val.strip() and is_grounded(val, vocab):
            val = val.strip()
            if limit is None or len(val) <= limit:
                return val, True
        return fallback, False

    subject, subj_llm = _grounded_or("subject", det_subject, limit=EMAIL_SUBJECT_LIMIT)
    body, body_llm = _grounded_or("body", det_body)
    return NetworkingEmail(subject, body, used_llm=subj_llm and body_llm)

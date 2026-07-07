"""Draft answers to standard application questions — real-data-only, pending review.

Same dependency-injected ``CompleteFn`` pattern as tailoring: the live path uses
Claude with a strict real-data-only guardrail and the cached résumé context; with no
API key it returns an empty draft (the application still gets stored as
``pending_review``, just without pre-written prose). Every answer is a draft for the
human to edit and send — nothing here is ever submitted automatically.
"""

from __future__ import annotations

from ..models import Job
from .llm import CompleteFn, resume_system_blocks
from .models import MasterResume

# Standard questions that recur across internship applications.
DEFAULT_QUESTIONS: list[str] = [
    "Why are you interested in this role and company?",
    "What relevant experience or projects make you a strong fit?",
    "Describe a technical challenge you solved and how.",
    "What are you hoping to learn or contribute during this internship?",
]

SYSTEM_INSTRUCTIONS = (
    "You draft first-draft answers to internship application questions for a candidate. "
    "STRICT RULES: use ONLY facts from the provided candidate profile — never invent "
    "experience, employers, metrics, skills, or interests the profile does not support. "
    "If the profile lacks the basis for a specific claim, keep the answer general and "
    "truthful rather than fabricating. Keep each answer to 2-4 concise sentences, first "
    "person, specific, and free of clichés.\n\n"
    'Respond with ONLY a JSON object: {"answers": {"<question>": "<answer>", ...}} using '
    "the exact question strings provided. No prose outside the JSON."
)


def build_system_blocks(resume: MasterResume) -> list[dict]:
    return resume_system_blocks(
        SYSTEM_INSTRUCTIONS, resume,
        label="CANDIDATE PROFILE (the only facts you may use)",
        include_bullets=True,
    )


def build_user_text(job: Job, keywords: list[str], questions: list[str]) -> str:
    lines = [
        f"JOB: {job.title} at {job.company_name}",
    ]
    if job.locations:
        lines.append("LOCATION: " + ", ".join(job.locations))
    if keywords:
        lines.append("JOB KEYWORDS: " + ", ".join(keywords))
    if job.description:
        lines.append("JOB DESCRIPTION:\n" + job.description)
    lines.append("")
    lines.append("Answer each of these questions:")
    lines.extend(f"- {q}" for q in questions)
    return "\n".join(lines)


def draft_common_answers(
    *,
    job: Job,
    keywords: list[str],
    resume: MasterResume,
    questions: list[str] | None = None,
    complete: CompleteFn | None = None,
) -> dict[str, str]:
    """Return ``{question: answer}`` drafts (empty dict if no LLM is configured)."""
    questions = questions or DEFAULT_QUESTIONS
    if complete is None:
        return {}

    system_blocks = build_system_blocks(resume)
    user_text = build_user_text(job, keywords, questions)
    data = complete(system_blocks, user_text)

    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        return {}
    # Keep only the questions we asked; coerce values to str.
    return {q: str(answers[q]) for q in questions if q in answers and answers[q]}

"""Draft answers to a job's REAL application questions — real-data-only, pending review.

Same dependency-injected ``CompleteFn`` pattern as tailoring: the live path uses
Claude with a strict real-data-only guardrail and the cached résumé context; with no
API key it returns an empty draft (the application still gets stored as
``pending_review``, just without pre-written prose). Every answer is a draft for the
human to edit and send — nothing here is ever submitted automatically.

Questions come from the ATS (``sourcing/questions.py`` — Greenhouse job-detail API).
There is no generic fallback question set: if a job's form questions aren't visible,
``prepare_applications`` skips drafting entirely instead of spending an LLM call on
answers nobody asked for. The model may return ``""`` for a question the profile
cannot truthfully answer (salary, referrals, eligibility, "how did you hear") —
empty answers are dropped here, so those land in the tracker for the human.
"""

from __future__ import annotations

from ..logging_config import get_logger
from ..models import Job
from .llm import CompleteFn, resume_system_blocks
from .models import MasterResume

log = get_logger(__name__)

SYSTEM_INSTRUCTIONS = (
    "<role>\n"
    "You draft first-draft answers to internship application questions, written in "
    "the candidate's own first-person voice. A human reviews and edits every answer "
    "before anything is submitted.\n"
    "</role>\n\n"
    "<strict_rules>\n"
    "These override any other instruction:\n"
    "1. Use ONLY facts from the provided candidate profile — never invent "
    "experience, employers, metrics, skills, or interests the profile does not "
    "support.\n"
    "2. If a question asks for something the profile cannot truthfully answer "
    "(salary expectations, referral names, work authorization, start dates, \"how "
    'did you hear about us\", references), return "" for that question — the human '
    "answers it. Never guess or fabricate.\n"
    "3. If the profile lacks the basis for a specific claim, keep the answer "
    "general and truthful rather than fabricating.\n"
    "</strict_rules>\n\n"
    "<answer_craft>\n"
    "- Answer the question directly in the first sentence — no restating the "
    "question, no throat-clearing.\n"
    "- Back the answer with ONE concrete project or experience from the profile: "
    "what the situation was, what the candidate did, and the real outcome.\n"
    "- Reference what this company/role actually does (from the job description) "
    "rather than generic praise that could apply to any company.\n"
    "- 2-4 sentences (about 40-90 words) per answer, unless the question clearly "
    "calls for more or less.\n"
    "- Plain, confident, specific. Banned clichés: \"passionate\", \"perfect fit\", "
    "\"fast-paced environment\", \"aligns with my values\", \"honed my skills\", "
    "\"excited to leverage\".\n"
    "</answer_craft>\n\n"
    "<output_format>\n"
    'Respond with ONLY a JSON object: {"answers": {"<question>": "<answer or empty '
    'string>", ...}} using the exact question strings provided. No prose outside '
    "the JSON.\n"
    "</output_format>"
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
    lines.append("Answer each of these application-form questions:")
    lines.extend(f"- {q}" for q in questions)
    return "\n".join(lines)


def draft_common_answers(
    *,
    job: Job,
    keywords: list[str],
    resume: MasterResume,
    questions: list[str],
    complete: CompleteFn | None = None,
) -> dict[str, str]:
    """Return ``{question: answer}`` drafts for the job's real form questions.

    Empty dict when there are no questions or no LLM is configured — no call is
    made. Answers the model returned as ``""`` (profile can't truthfully answer)
    are dropped, leaving those questions to the human.
    """
    if not questions or complete is None:
        return {}

    system_blocks = build_system_blocks(resume)
    user_text = build_user_text(job, keywords, questions)
    try:
        data = complete(system_blocks, user_text)
    except Exception as exc:  # one bad LLM response must not kill the whole stage
        log.warning(
            "answer drafting LLM call failed; leaving the questions to the human",
            extra={"company": job.company_name, "error": repr(exc)},
        )
        return {}

    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        return {}
    # Keep only the questions we asked; coerce values to str; drop empty answers.
    return {q: str(answers[q]) for q in questions if q in answers and answers[q]}

"""Assemble the tailored one-page CV document and render it to PDF via LaTeX.

The tailoring step chooses/reorders real bullets; here we regroup them under
their parent experience/project and emit a renderer-agnostic cv-doc dict. The
YAML serialization of that dict is what storage keeps (``tailored_resume_yaml``,
``cv_cache``) — the auditable, re-renderable source. The PDF itself is produced
from the dict by ``resume/latex.py``, replicating the user's own ``Resume.tex``
template; with no LaTeX engine installed the YAML/.tex are still written and the
PDF is skipped, so the pipeline degrades gracefully.

One-page guarantee: ``write_and_render_one_page`` starts from the full tailored
bullet list (deliberately generous — a full page beats a sparse one) and, if the
compiled PDF overflows one page, drops the LEAST relevant bullet (tailoring
returns them priority-ordered) and re-renders until it fits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml

from ..logging_config import get_logger
from .latex import build_latex, compile_latex, find_latex_engine, pdf_page_count
from .models import MasterResume
from .tailoring import TailoredBullet

log = get_logger(__name__)

# Bullets a one-page trim will never cut below — a résumé with almost nothing
# left is a signal something else is wrong (e.g. giant master sections), not
# something to "fix" by deleting the candidate's last accomplishments.
_MIN_BULLETS = 4


def _username(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    seg = [s for s in urlsplit(url).path.split("/") if s]
    return seg[-1] if seg else None


def _social_networks(resume: MasterResume) -> list[dict]:
    out: list[dict] = []
    if (u := _username(resume.links.linkedin)):
        out.append({"network": "LinkedIn", "username": u})
    if (u := _username(resume.links.github)):
        out.append({"network": "GitHub", "username": u})
    return out


def build_cv_doc(
    resume: MasterResume, tailored: list[TailoredBullet], *, is_canadian: bool = False
) -> dict:
    """Build the cv-doc (``{"cv": ...}``) as a dict.

    Only experiences/projects that contributed at least one tailored bullet are
    included; education/skills/header render verbatim from the master résumé.
    ``is_canadian`` picks the citizenship line appended to the summary — see
    ``resume.matching.is_canadian_job`` for how a job earns that flag.
    """
    # Regroup tailored bullets under their parent ENTRY (by index, not company/
    # project name — two experiences at the same company must not merge).
    exp_bullets: dict[int, list[str]] = {}
    proj_bullets: dict[int, list[str]] = {}
    for tb in tailored:
        bucket = exp_bullets if tb.ref.source == "experience" else proj_bullets
        bucket.setdefault(tb.ref.parent_index, []).append(tb.text)

    sections: dict[str, list] = {}

    citizenship = (
        (resume.citizenship_canada or resume.citizenship) if is_canadian else resume.citizenship
    )
    citizenship_sentence = f"{citizenship}." if citizenship else None
    summary_line = " ".join(p for p in (resume.summary, citizenship_sentence) if p)
    if summary_line:
        sections["summary"] = [summary_line]

    if resume.education:
        sections["education"] = [
            {
                k: v
                for k, v in {
                    "institution": edu.institution,
                    "area": edu.area,
                    "degree": edu.degree,
                    "location": edu.location,
                    "start_date": edu.start_date,
                    "end_date": edu.end_date,
                    "highlights": edu.highlights or None,
                }.items()
                if v is not None
            }
            for edu in resume.education
        ]

    experience_entries = []
    for ei, exp in enumerate(resume.experiences):
        highlights = exp_bullets.get(ei)
        if not highlights:  # only include experiences that contributed a tailored bullet
            continue
        experience_entries.append(
            {
                k: v
                for k, v in {
                    "company": exp.company,
                    "position": exp.role,
                    "location": exp.location,
                    "start_date": exp.start_date,
                    "end_date": exp.end_date,
                    "highlights": highlights,
                }.items()
                if v is not None
            }
        )
    if experience_entries:
        sections["experience"] = experience_entries

    project_entries = []
    for pi, proj in enumerate(resume.projects):
        highlights = proj_bullets.get(pi)
        if not highlights:
            continue
        # A linked project's name renders as the template's blue \repolink.
        name = f"[{proj.name}]({proj.url})" if proj.url else proj.name
        project_entries.append(
            {
                k: v
                for k, v in {
                    "name": name,
                    "start_date": proj.start_date,
                    "end_date": proj.end_date,
                    "highlights": highlights,
                }.items()
                if v is not None
            }
        )
    if project_entries:
        sections["projects"] = project_entries

    skill_rows = []
    if resume.skills.languages:
        skill_rows.append({"label": "Languages", "details": ", ".join(resume.skills.languages)})
    if resume.skills.frameworks:
        skill_rows.append({"label": "Frameworks", "details": ", ".join(resume.skills.frameworks)})
    if resume.skills.tools:
        skill_rows.append({"label": "Tools", "details": ", ".join(resume.skills.tools)})
    if skill_rows:
        sections["skills"] = skill_rows

    cv: dict = {"name": resume.name}
    for key, value in {
        "email": resume.email,
        "phone": resume.phone,
        "location": resume.location,
        "website": resume.links.website,
    }.items():
        if value:
            cv[key] = value
    socials = _social_networks(resume)
    if socials:
        cv["social_networks"] = socials
    cv["sections"] = sections

    return {"cv": cv}


def to_yaml(cv_doc: dict) -> str:
    """Serialize the cv-doc to YAML (deterministic key order)."""
    return yaml.safe_dump(cv_doc, sort_keys=False, allow_unicode=True)


def write_and_render(cv_doc: dict, out_dir: str, slug: str) -> tuple[str, Optional[str]]:
    """Write ``<slug>.yaml`` + ``<slug>.tex`` and compile the PDF.

    Returns ``(yaml_path, pdf_path_or_None)``. YAML and .tex are always written;
    the PDF is produced only when a LaTeX engine is available and succeeds.
    """
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    yaml_path = out / f"{slug}.yaml"
    yaml_path.write_text(to_yaml(cv_doc), encoding="utf-8")

    tex_path = out / f"{slug}.tex"
    tex_path.write_text(build_latex(cv_doc), encoding="utf-8")

    pdf_path = compile_latex(str(tex_path))
    if pdf_path:
        log.info("rendered résumé PDF", extra={"pdf": pdf_path})
    return str(yaml_path), pdf_path


@dataclass
class OnePageRender:
    """Result of a fit-to-one-page render."""

    cv_doc: dict
    yaml_path: str
    pdf_path: Optional[str]
    bullets: list[TailoredBullet]  # the bullets that actually made the page
    dropped: int = 0  # bullets trimmed to reach one page
    pages: Optional[int] = None  # page count of the final PDF (None = unknown)


def write_and_render_one_page(
    resume: MasterResume,
    bullets: list[TailoredBullet],
    out_dir: str,
    slug: str,
    *,
    max_pages: int = 1,
    is_canadian: bool = False,
) -> OnePageRender:
    """Render, then trim least-relevant-last until the PDF fits ``max_pages``.

    ``bullets`` must be priority-ordered (as ``tailor_resume`` returns them):
    the trim always drops the LAST bullet. Without an engine (or a readable
    page count) the first render is returned untrimmed — the review app / human
    still sees the artifacts. ``is_canadian`` passes straight through to
    ``build_cv_doc`` (the citizenship line).
    """
    kept = list(bullets)
    doc = build_cv_doc(resume, kept, is_canadian=is_canadian)
    yaml_path, pdf_path = write_and_render(doc, out_dir, slug)

    if pdf_path is None or find_latex_engine() is None:
        return OnePageRender(cv_doc=doc, yaml_path=yaml_path, pdf_path=pdf_path, bullets=kept)

    pages = pdf_page_count(pdf_path)
    dropped = 0
    while pages is not None and pages > max_pages and len(kept) > _MIN_BULLETS:
        kept = kept[:-1]
        dropped += 1
        doc = build_cv_doc(resume, kept, is_canadian=is_canadian)
        yaml_path, pdf_path = write_and_render(doc, out_dir, slug)
        if pdf_path is None:
            break
        pages = pdf_page_count(pdf_path)

    if dropped:
        log.info(
            "trimmed bullets to fit one page",
            extra={"slug": slug, "dropped": dropped, "kept": len(kept), "pages": pages},
        )
    return OnePageRender(
        cv_doc=doc, yaml_path=yaml_path, pdf_path=pdf_path,
        bullets=kept, dropped=dropped, pages=pages,
    )

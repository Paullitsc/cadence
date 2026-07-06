"""Assemble a one-page RenderCV YAML from the tailored bullets and render a PDF.

The tailoring step chooses/reorders real bullets; here we regroup them under their
parent experience/project and emit a RenderCV ``cv``/``design`` document. Education
and skills are rendered verbatim from the master résumé. Rendering is one CLI call
(``rendercv render``); if RenderCV is not installed we still write the YAML and skip
the PDF, so the pipeline degrades gracefully.

Links: RenderCV renders Markdown ``[text](url)`` in names/highlights as real clickable
PDF links (verified against rendercv 2.8 → typst ``#link``). A project with a ``url``
therefore gets its NAME emitted as a Markdown link (the bare ``url:`` key on an entry
is silently ignored by RenderCV — it is NOT the way to link). Markdown links written
inside bullet text in ``master_resume.yaml`` flow through tailoring verbatim and render
clickable too.

Schema note: entry keys (institution/area/degree; company/position/highlights;
label/details; social_networks network/username) confirmed against rendercv 2.8
(`rendercv.schema.models.cv.section`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml

from ..logging_config import get_logger
from .models import MasterResume
from .tailoring import TailoredBullet

log = get_logger(__name__)


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


def build_rendercv_cv(resume: MasterResume, tailored: list[TailoredBullet]) -> dict:
    """Build the RenderCV document (``{"cv": ..., "design": ...}``) as a dict."""
    # Regroup tailored bullets under their parent, preserving tailored order.
    exp_bullets: dict[str, list[str]] = {}
    proj_bullets: dict[str, list[str]] = {}
    for tb in tailored:
        bucket = exp_bullets if tb.ref.source == "experience" else proj_bullets
        bucket.setdefault(tb.ref.parent, []).append(tb.text)

    sections: dict[str, list] = {}

    if resume.summary:
        sections["summary"] = [resume.summary]

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
    for exp in resume.experiences:
        highlights = exp_bullets.get(exp.company)
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
    for proj in resume.projects:
        highlights = proj_bullets.get(proj.name)
        if not highlights:
            continue
        # A linked project renders its name as a clickable Markdown link. (A bare
        # `url:` key is ignored by RenderCV entries — the link must be in the text.)
        name = f"[{proj.name}]({proj.url})" if proj.url else proj.name
        project_entries.append({"name": name, "highlights": highlights})
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

    return {"cv": cv, "design": {"theme": "classic"}}


def to_yaml(cv_doc: dict) -> str:
    """Serialize the RenderCV document to YAML (deterministic key order)."""
    return yaml.safe_dump(cv_doc, sort_keys=False, allow_unicode=True)


def write_and_render(cv_doc: dict, out_dir: str, slug: str) -> tuple[str, Optional[str]]:
    """Write ``<slug>.yaml`` and render a PDF via the RenderCV CLI.

    Returns ``(yaml_path, pdf_path_or_None)``. The YAML is always written; the PDF is
    produced only if the ``rendercv`` CLI is available and the render succeeds.
    """
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    yaml_path = out / f"{slug}.yaml"
    yaml_path.write_text(to_yaml(cv_doc), encoding="utf-8")

    if shutil.which("rendercv") is None:
        log.info("rendercv CLI not found; wrote YAML only", extra={"yaml": str(yaml_path)})
        return str(yaml_path), None

    # Pin the PDF to <slug>.pdf so every job keeps its OWN artifact — the default
    # (rendercv_output/<Name>_CV.pdf) is named after the person and would be
    # overwritten by each subsequent job in the same run. Skip md/html/png side
    # outputs; the tracker only needs the PDF (+ the YAML written above).
    pdf_target = out / f"{slug}.pdf"
    try:
        proc = subprocess.run(
            [
                "rendercv", "render", yaml_path.name,
                "--pdf-path", pdf_target.name,  # relative to the input file
                "-nomd", "-nohtml", "-nopng", "-q",
            ],
            cwd=str(out),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("rendercv render failed to run; wrote YAML only", extra={"error": repr(exc)})
        return str(yaml_path), None

    if proc.returncode != 0:
        log.warning(
            "rendercv render returned non-zero; wrote YAML only",
            extra={"returncode": proc.returncode, "stderr": (proc.stderr or "")[-500:]},
        )
        return str(yaml_path), None

    pdf_path = str(pdf_target) if pdf_target.exists() else None
    log.info("rendered résumé PDF", extra={"pdf": pdf_path})
    return str(yaml_path), pdf_path

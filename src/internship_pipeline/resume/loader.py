"""Load and index the tagged master résumé YAML.

Pure I/O + parsing (no network, no LLM). Assigns each bullet a stable id so the
tailoring step can reference bullets without emitting free-form text.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from ..logging_config import get_logger
from .models import BulletRef, MasterResume

log = get_logger(__name__)


def _stable_bullet_id(source: str, parent: str, text: str) -> str:
    """Content-derived id: same (source, parent, text) always hashes the same.

    IDs used to encode list position (``p{project_index}b{bullet_index}``), which
    silently broke every bullet id in and after the wrong slot whenever
    ``master_resume.yaml`` was edited (a project inserted/reordered) between when
    an application's ``recommended_bullets`` were tailored and when it was later
    (re-)rendered — the id still resolved to *a* bullet, just the wrong one, so a
    stale résumé could show one project's bullet under a different project's
    heading with no error. Hashing the content instead of the position makes an
    id survive any edit that doesn't touch that exact bullet/parent.
    """
    digest = hashlib.sha256(f"{source}:{parent}:{text}".encode("utf-8")).hexdigest()[:12]
    prefix = "e" if source == "experience" else "p"
    return f"{prefix}{digest}"


def load_master_resume(path: str) -> MasterResume:
    """Parse the master résumé YAML into a validated ``MasterResume``.

    Raises ``FileNotFoundError`` if the file is missing (callers decide whether to
    skip gracefully) and ``ValueError`` on malformed YAML.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"master résumé not found: {path}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"master résumé must be a YAML mapping, got {type(raw).__name__}")
    resume = MasterResume.model_validate(raw)
    if resume.placeholder:
        log.warning(
            "master résumé is placeholder content; tailored output is not real",
            extra={"path": str(p)},
        )
    return resume


def all_bullets(resume: MasterResume) -> list[BulletRef]:
    """Flatten every experience/project bullet into referenceable ``BulletRef``s."""
    refs: list[BulletRef] = []
    for ei, exp in enumerate(resume.experiences):
        for bullet in exp.bullets:
            refs.append(
                BulletRef(
                    id=_stable_bullet_id("experience", exp.company, bullet.text),
                    text=bullet.text,
                    tags=bullet.tags,
                    metrics=bullet.metrics,
                    source="experience",
                    parent=exp.company,
                    parent_index=ei,
                )
            )
    for pi, proj in enumerate(resume.projects):
        for bullet in proj.bullets:
            refs.append(
                BulletRef(
                    id=_stable_bullet_id("project", proj.name, bullet.text),
                    text=bullet.text,
                    tags=bullet.tags,
                    metrics=bullet.metrics,
                    source="project",
                    parent=proj.name,
                    parent_index=pi,
                )
            )
    return refs

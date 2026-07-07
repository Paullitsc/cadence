"""Load and index the tagged master résumé YAML.

Pure I/O + parsing (no network, no LLM). Assigns each bullet a stable id so the
tailoring step can reference bullets without emitting free-form text.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..logging_config import get_logger
from .models import BulletRef, MasterResume

log = get_logger(__name__)


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
        for bi, bullet in enumerate(exp.bullets):
            refs.append(
                BulletRef(
                    id=f"e{ei}b{bi}",
                    text=bullet.text,
                    tags=bullet.tags,
                    metrics=bullet.metrics,
                    source="experience",
                    parent=exp.company,
                    parent_index=ei,
                )
            )
    for pi, proj in enumerate(resume.projects):
        for bi, bullet in enumerate(proj.bullets):
            refs.append(
                BulletRef(
                    id=f"p{pi}b{bi}",
                    text=bullet.text,
                    tags=bullet.tags,
                    metrics=bullet.metrics,
                    source="project",
                    parent=proj.name,
                    parent_index=pi,
                )
            )
    return refs

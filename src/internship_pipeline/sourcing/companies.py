"""Load and validate ``companies.yaml`` into typed targets.

Schema is documented in ``companies.example.yaml``. Placeholder rows (slug still a
``REPLACE_ME*`` value) and malformed rows are skipped with a log line rather than
crashing the run — sourcing must be defensive (blueprint: "skip-on-error").
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, field_validator

from ..logging_config import get_logger

log = get_logger(__name__)

Ats = Literal["greenhouse", "lever", "ashby"]


class CompanyTarget(BaseModel):
    """One target company + its ATS board token."""

    name: str
    ats: Ats
    slug: str

    @field_validator("slug", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def is_placeholder(self) -> bool:
        return not self.slug or self.slug.upper().startswith("REPLACE_ME")


def load_companies(
    path: str | Path, *, fallback: Optional[str | Path] = None
) -> list[CompanyTarget]:
    """Parse the companies file into validated, non-placeholder targets.

    Falls back to ``fallback`` (e.g. the committed example) when ``path`` is
    missing, so a fresh checkout still runs. Returns ``[]`` if neither exists.
    """
    p = Path(path)
    if not p.exists() and fallback is not None and Path(fallback).exists():
        log.warning(
            "companies file missing; using fallback",
            extra={"path": str(p), "fallback": str(fallback)},
        )
        p = Path(fallback)
    if not p.exists():
        log.warning("no companies file found", extra={"path": str(path)})
        return []

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rows = raw.get("companies") or []
    targets: list[CompanyTarget] = []
    for i, row in enumerate(rows):
        try:
            target = CompanyTarget(**row)
        except Exception as exc:  # malformed entry: skip, don't crash the run
            log.warning(
                "skipping invalid company row", extra={"index": i, "error": repr(exc)}
            )
            continue
        if target.is_placeholder:
            log.info("skipping placeholder company", extra={"company": target.name})
            continue
        targets.append(target)

    log.info(
        "loaded companies", extra={"path": str(p), "count": len(targets)}
    )
    return targets

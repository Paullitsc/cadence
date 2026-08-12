"""Load ``blocked_companies.yaml`` — companies to drop before they're stored.

Schema is documented in ``blocked_companies.example.yaml``. Missing/empty file is
not an error (nothing blocked) — same graceful-degrade as every other optional
YAML config in this repo.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..logging_config import get_logger

log = get_logger(__name__)


def load_blocked_companies(path: str | Path) -> frozenset[str]:
    """Parse the blocklist into a set of lowercased, stripped company names."""
    p = Path(path)
    if not p.exists():
        return frozenset()

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rows = raw.get("companies") or []
    return frozenset(str(name).strip().lower() for name in rows if str(name).strip())

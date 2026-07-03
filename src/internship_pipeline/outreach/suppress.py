"""Manage + consult the do-not-contact suppression list (Phase 3).

    python -m internship_pipeline.outreach.suppress add a@b.com --reason "asked to stop"
    python -m internship_pipeline.outreach.suppress add competitor.com   # a whole domain
    python -m internship_pipeline.outreach.suppress list

An entry is a full email address or a bare domain (opt out an entire company). The
send gate checks this list before every send, so adding someone here permanently
blocks outreach to them.

Two sources are merged (config: ``OUTREACH_SUPPRESSION_FILE`` "merged with the
DB-backed list"): the DB table managed above, and an optional plain-text seed file
of emails/domains (one per line, ``#`` comments allowed). ``is_suppressed`` is the
single check both the draft stage and the send gate use, so the rule is identical.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path

from ..config import Settings, get_settings
from ..logging_config import configure_logging, get_logger
from ..storage import Storage, get_storage
from ..storage.base import suppression_matches

log = get_logger(__name__)


@lru_cache(maxsize=8)
def _read_seed(path: str) -> tuple[str, ...]:
    """Read + normalize a suppression seed file (cached by path). Missing file = empty."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("suppression seed file not found; ignoring", extra={"path": path})
        return ()
    entries = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if line:
            entries.append(line)
    return tuple(entries)


def load_suppression_seed(settings: Settings) -> list[str]:
    """Entries from the optional ``OUTREACH_SUPPRESSION_FILE`` (empty when unset)."""
    path = (settings.outreach_suppression_file or "").strip()
    return list(_read_seed(path)) if path else []


def is_suppressed(email: str, storage: Storage, settings: Settings) -> bool:
    """True if ``email`` (or its domain) is on the DB list OR the seed file."""
    if not (email or "").strip():
        return False
    if storage.is_suppressed(email):
        return True
    return suppression_matches(email, load_suppression_seed(settings))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the outreach suppression (do-not-contact) list.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add an email or domain to the suppression list")
    add.add_argument("entry", help="email address or bare domain")
    add.add_argument("--reason", default=None)
    sub.add_parser("list", help="list all suppression entries (DB + seed file)")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    storage = get_storage(settings)
    try:
        if args.command == "add":
            storage.add_suppression(args.entry, args.reason)
            print(f"suppressed: {args.entry.strip().lower()}")
        elif args.command == "list":
            entries = set(storage.list_suppressions()) | set(load_suppression_seed(settings))
            if not entries:
                print("(suppression list is empty)")
            for e in sorted(entries):
                print(e)
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

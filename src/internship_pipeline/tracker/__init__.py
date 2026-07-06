"""Phase 5: Google Sheets application tracker + Google Drive CV store.

The sheet is the human's application WORKSPACE — one row per prepared application
(job link, durable Drive CV link, drafted answers, a human-owned status/notes
workflow). Storage (Supabase/SQLite) stays the source of truth; the sheet is a
projection of it.

Same split as ``sourcing/`` and ``outreach/``: **row building/diffing is pure**
(``rows.py`` — fixture-testable, no network); the Google API calls live in thin
wrappers (``sheets.py``, ``drive.py``) behind lazy imports, so the pipeline and the
test suite run offline with zero credentials — with no service account configured
the sync stage logs one line and no-ops.
"""

from __future__ import annotations

from .auth import build_tracker_services, tracker_configured
from .rows import (
    ANSWERS_HEADERS,
    HEADERS,
    STATUS_OPTIONS,
    SheetPlan,
    plan_answers_upsert,
    plan_applications_upsert,
    spreadsheet_url,
)

__all__ = [
    "build_tracker_services",
    "tracker_configured",
    "ANSWERS_HEADERS",
    "HEADERS",
    "STATUS_OPTIONS",
    "SheetPlan",
    "plan_answers_upsert",
    "plan_applications_upsert",
    "spreadsheet_url",
]

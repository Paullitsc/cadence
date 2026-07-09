"""Local CV review app — the human-in-the-loop step of the CV workflow.

``python -m internship_pipeline.review`` serves a localhost UI over the pending
applications in storage. The AI handles everything except the experience/projects
selection: header, education and skills render verbatim from the master résumé,
and the AI's recommended experience/project bullets come PRE-CHECKED. The human
toggles bullets, previews the compiled one-page PDF (with a live page count, so
it visibly fits), and submits — only then is the CV finalized (rendered, uploaded
to Drive when configured) and the application row pushed to the tracker sheet.

Split mirrors the rest of the codebase: ``selection.py`` is pure (fixture-
testable — recommendation prechecking, selection→bullets assembly); ``app.py``
owns the HTTP server and the side effects (render, Drive, Sheets, storage).
"""

from __future__ import annotations

from .selection import EntryOptions, entry_options, selection_to_bullets

__all__ = ["EntryOptions", "entry_options", "selection_to_bullets"]

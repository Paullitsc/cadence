"""Thin Google Drive wrapper: upload tailored-CV PDFs to the shared folder.

Why Drive at all: tailored PDFs are rendered on the ephemeral GitHub Actions runner
and destroyed with it (uploading them as Actions artifacts is banned — public repo,
PII). Drive is the durable artifact store; the ``webViewLink`` is what the tracker
sheet shows.

Uploads are idempotent by file NAME within the folder (``<dedupe_key>.pdf``): a
re-run updates the existing file's content instead of piling up duplicates, and the
link stays stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..logging_config import get_logger

log = get_logger(__name__)


@dataclass
class DriveFile:
    """The stored artifact: what goes on the Application row / cv_cache entry."""

    file_id: str
    web_view_link: str


def find_file(drive: Any, folder_id: str, name: str) -> Optional[DriveFile]:
    """Look up a file by exact name inside the folder (None if absent)."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    resp = (
        drive.files()
        .list(
            q=f"name = '{escaped}' and '{folder_id}' in parents and trashed = false",
            fields="files(id, webViewLink)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files", [])
    if not files:
        return None
    return DriveFile(file_id=files[0]["id"], web_view_link=files[0].get("webViewLink", ""))


def upload_pdf(drive: Any, folder_id: str, pdf_path: str, name: str) -> Optional[DriveFile]:
    """Upload (or update) one PDF in the folder. Returns None on any failure —
    a lost upload must never fail the run; the local path still exists for local runs."""
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        log.warning("google api libraries not installed; skipping Drive upload")
        return None
    try:
        media = MediaFileUpload(pdf_path, mimetype="application/pdf", resumable=False)
        existing = find_file(drive, folder_id, name)
        if existing is not None:
            updated = (
                drive.files()
                .update(
                    fileId=existing.file_id,
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return DriveFile(
                file_id=updated["id"], web_view_link=updated.get("webViewLink", "")
            )
        created = (
            drive.files()
            .create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return DriveFile(file_id=created["id"], web_view_link=created.get("webViewLink", ""))
    except Exception as exc:  # skip-on-error: the pipeline keeps the local artifact
        log.warning(
            "Drive upload failed; continuing without a durable link",
            extra={"pdf": pdf_path, "error": repr(exc)},
        )
        return None

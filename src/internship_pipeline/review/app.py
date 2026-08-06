"""The review app's HTTP server (stdlib only — no new runtime dependency).

Single-threaded on purpose: requests are quick page builds or one LaTeX compile,
the app is single-user local tooling, and serializing requests keeps SQLite and
the preview files race-free. Endpoints:

    GET  /                    pending + reviewed + discarded application lists
    GET  /review/<key>        the selection UI for one application
    POST /api/preview         {key, ids} → compile the selection, report pages
    GET  /preview/<key>.pdf   the last compiled preview for that application
    POST /api/submit          {key, ids} → finalize: render, Drive upload,
                              storage update (status → "reviewed"), sheet row
    POST /api/discard         {key} → status "withdrawn" without ever reviewing
    POST /api/restore         {key} → a discarded application back to pending

Previews compile EXACTLY the human's selection (no auto-trim) so the page count
shown is the truth about their choice; the submit likewise renders what was
approved.
"""

from __future__ import annotations

import html
import json
import tempfile
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ..config import Settings
from ..logging_config import get_logger
from ..models import Application
from ..resume import build_cv_doc, load_master_resume, to_yaml, write_and_render
from ..resume.latex import find_latex_engine, pdf_page_count
from ..resume.matching import is_canadian_job
from ..resume.models import MasterResume
from ..storage import get_storage
from ..tracker.auth import TrackerServices, build_tracker_services, tracker_configured
from ..tracker.drive import upload_pdf
from ..tracker.sync import sync_applications_to_sheet
from .selection import entry_options, selection_to_bullets

log = get_logger(__name__)

_FULL_PAGE_BULLETS = 16  # what a full Resume.tex-style page typically holds


class ReviewApp:
    """State + actions behind the HTTP handler (constructed once per server)."""

    def __init__(self, settings: Settings, storage, resume: MasterResume) -> None:
        self.settings = settings
        self.storage = storage
        self.resume = resume
        self.preview_dir = tempfile.mkdtemp(prefix="cv-review-")
        self._services: Optional[TrackerServices] = None
        self._services_built = False

    # --- side-effecting actions -------------------------------------------

    def tracker_services(self) -> Optional[TrackerServices]:
        """Build the Sheets/Drive clients once, lazily (None when unconfigured)."""
        if not self._services_built:
            self._services_built = True
            if tracker_configured(self.settings):
                self._services = build_tracker_services(self.settings)
        return self._services

    def _is_canadian(self, key: str) -> bool:
        """Does the job's location call for the Canadian citizenship line?"""
        job = self.storage.get_job(key)
        return is_canadian_job(job) if job is not None else False

    def preview(self, key: str, ids: list[str]) -> dict:
        app = self.storage.get_application(key)
        if app is None:
            return {"error": "unknown application"}
        bullets = selection_to_bullets(self.resume, app, ids)
        if not bullets:
            return {"error": "select at least one bullet"}
        doc = build_cv_doc(self.resume, bullets, is_canadian=self._is_canadian(key))
        _, pdf_path = write_and_render(doc, self.preview_dir, key)
        pages = pdf_page_count(pdf_path) if pdf_path else None
        return {
            "bullets": len(bullets),
            "pdf": bool(pdf_path),
            "pages": pages,
            "engine": find_latex_engine(),
        }

    def submit(self, key: str, ids: list[str]) -> dict:
        app = self.storage.get_application(key)
        if app is None:
            return {"error": "unknown application"}
        bullets = selection_to_bullets(self.resume, app, ids)
        if not bullets:
            return {"error": "select at least one bullet"}

        doc = build_cv_doc(self.resume, bullets, is_canadian=self._is_canadian(key))
        yaml_path, pdf_path = write_and_render(doc, self.settings.resume_output_dir, key)

        drive_link = None
        services = self.tracker_services()
        if services and services.drive and self.settings.drive_folder_id and pdf_path:
            uploaded = upload_pdf(
                services.drive, self.settings.drive_folder_id, pdf_path, f"{key}.pdf"
            )
            if uploaded is not None:
                drive_link = uploaded.web_view_link

        app.final_bullets = [{"id": tb.ref.id, "text": tb.text} for tb in bullets]
        app.tailored_resume_yaml = to_yaml(doc)
        app.tailored_resume_path = pdf_path or yaml_path
        if drive_link:
            app.cv_drive_link = drive_link
        app.reviewed_at = datetime.now(timezone.utc).isoformat()
        app.status = "reviewed"
        self.storage.save_application(app)

        sheet_synced = False
        sheet_error = None
        if services is None and tracker_configured(self.settings):
            # TRACKER_SHEETS_ENABLED/GOOGLE_SERVICE_ACCOUNT_JSON/SHEETS_SPREADSHEET_ID
            # are all set, so this isn't "unconfigured" — the client itself failed to
            # build (missing google-api-python-client/google-auth, a bad service-account
            # file, ...). See the server log for the exact reason.
            sheet_error = (
                "tracker is configured but its client failed to build — check the "
                "server terminal for the reason (often `uv sync --extra tracker "
                "--extra gmail`)"
            )
        elif services is not None:
            try:
                locations = {}
                job = self.storage.get_job(key)
                if job is not None and job.locations:
                    locations[key] = job.locations
                cv_links = {
                    a.dedupe_key: a.cv_drive_link
                    for a in self.storage.list_applications()
                    if a.cv_drive_link
                }
                sync_applications_to_sheet(
                    services,
                    self.settings.sheets_spreadsheet_id or "",
                    [app],
                    storage=self.storage,
                    locations_by_key=locations,
                    cv_links_by_key=cv_links,
                )
                sheet_synced = True
            except Exception as exc:  # storage already holds the review; sync can retry daily
                log.warning("sheet sync failed on submit", extra={"key": key, "error": repr(exc)})
                sheet_error = repr(exc)

        log.info(
            "application reviewed",
            extra={"key": key, "bullets": len(bullets), "drive": bool(drive_link),
                   "sheet_synced": sheet_synced},
        )
        return {
            "ok": True,
            "pages": pdf_page_count(pdf_path) if pdf_path else None,
            "pdf_path": pdf_path,
            "drive_link": drive_link,
            "sheet_synced": sheet_synced,
            "sheet_error": sheet_error,
        }

    def _set_status(self, key: str, status: str) -> tuple[bool, Optional[str]]:
        """``(changed?, error)`` for one application — the shared discard/restore step.

        ``allowed_from`` lives in the callers: this only writes and reports.
        """
        app = self.storage.get_application(key)
        if app is None:
            return False, "unknown application"
        if app.status == status:
            return False, None  # already there (double-click / re-submitted batch)
        if status == "withdrawn" and app.status != "pending_review":
            # A reviewed application already owns a sheet row, and that row is
            # deleted by the sheet's own Status dropdown (`sync_tracker`); doing
            # it here would orphan the row.
            return False, (
                f"{app.company_name} — {app.title}: is {app.status}, not pending. Set "
                "its Status to 'withdrawn' in the tracker sheet so the row goes too."
            )
        if status == "pending_review" and app.status != "withdrawn":
            return False, f"{app.company_name} — {app.title}: is {app.status}; nothing to restore."
        app.status = status
        self.storage.save_application(app)
        log.info(
            "application status set by review app",
            extra={"key": key, "status": status, "company": app.company_name},
        )
        return True, None

    def discard(self, keys: list[str]) -> dict:
        """Drop jobs the human doesn't want, straight from the pending list.

        Batched: the UI picks a set of rows and sends them in one request. Only
        ``pending_review`` applications qualify — they were never pushed to the
        sheet (only reviewed ones are), so recording ``withdrawn`` in storage is
        the whole job, and ``match_and_slice`` only ever prepares NEW jobs, so
        they can't come back.

        Partial failure is fine and reported: every key is attempted, the ones
        that couldn't be discarded come back in ``errors`` for the UI to show.
        """
        discarded = 0
        errors: list[str] = []
        for key in dict.fromkeys(keys):
            changed, error = self._set_status(key, "withdrawn")
            discarded += int(changed)
            if error:
                errors.append(error)
        return {"ok": not errors, "discarded": discarded, "errors": errors}

    def restore(self, keys: list[str]) -> dict:
        """Undo discards: back to ``pending_review`` (the review flow untouched)."""
        restored = 0
        errors: list[str] = []
        for key in dict.fromkeys(keys):
            changed, error = self._set_status(key, "pending_review")
            restored += int(changed)
            if error:
                errors.append(error)
        return {"ok": not errors, "restored": restored, "errors": errors}

    # --- page builds --------------------------------------------------------

    def index_html(self) -> str:
        pending = self.storage.list_applications(status="pending_review")
        reviewed = self.storage.list_applications(status="reviewed")
        discarded = self.storage.list_applications(status="withdrawn")

        def rows(apps: list[Application], mode: str) -> str:
            """One table body. ``pending``/``discarded`` rows are pickable (the bulk
            Discard/Restore bar reads their checkboxes); ``reviewed`` rows are not."""
            pickable = mode in ("pending", "discarded")
            if not apps:
                return f'<tr><td colspan="{6 if pickable else 5}" class="empty">none</td></tr>'
            out = []
            for a in apps:
                key = html.escape(a.dedupe_key)
                pick = (
                    f'<td class="pick"><input type="checkbox" class="rowpick" value="{key}" '
                    f"onchange=\"updatePick('{mode}')\"></td>"
                    if pickable
                    else ""
                )
                if mode == "discarded":
                    actions = (
                        f"<button class=\"secondary rowbtn\" onclick=\"startPick('discarded','{key}')\">"
                        "Restore</button>"
                    )
                else:
                    label = "Review" if mode == "pending" else "Reopen"
                    actions = f'<a class="btn" href="/review/{key}">{label}</a>'
                    if mode == "pending":
                        # Doesn't discard on the spot: it arms the bulk picker with
                        # this row ticked, so triaging one job and triaging ten are
                        # the same gesture.
                        actions += (
                            f" <button class=\"danger rowbtn\" onclick=\"startPick('pending','{key}')\">"
                            "Discard</button>"
                        )
                # Full extracted keyword list (~20): this is the human's triage
                # view, and seeing what the JD wants is how they pick what to
                # customize — truncating it hid the signal.
                kw = ", ".join(a.keywords)
                out.append(
                    "<tr>"
                    f"{pick}"
                    f"<td>{html.escape(a.company_name)}</td>"
                    f'<td><a href="{html.escape(a.url)}" target="_blank" rel="noopener">'
                    f"{html.escape(a.title)}</a></td>"
                    f"<td>{a.fit_score:.2f}</td>"
                    f"<td class=kw>{html.escape(kw)}</td>"
                    f"<td class=act>{actions}</td>"
                    "</tr>"
                )
            return "".join(out)

        def section(apps: list[Application], mode: str, heading: str, verb: str, hint: str) -> str:
            """A pickable list: heading, bulk bar (only when there's something to
            pick), select-all header checkbox, rows."""
            bar = (
                _PICK_BAR.format(mode=mode, verb=verb, start=f"{verb} several…", hint=hint)
                if apps
                else ""
            )
            head = (
                '<th class="pick"><input type="checkbox" class="allpick" '
                f"onchange=\"toggleAll('{mode}', this)\"></th>{_LIST_HEAD}"
            )
            return _LIST_SECTION.format(
                mode=mode, heading=heading, bar=bar, head=head, rows=rows(apps, mode)
            )

        discarded_section = ""
        if discarded:
            discarded_section = section(
                discarded,
                "discarded",
                f"Discarded ({len(discarded)}) — never sent to the sheet",
                "Restore",
                "tick the ones to put back in the pending list",
            )

        return _INDEX_TEMPLATE.format(
            css=_CSS,
            pending_count=len(pending),
            reviewed_count=len(reviewed),
            pending_section=section(
                pending,
                "pending",
                "Pending review",
                "Discard",
                "tick every job you want gone, then hit Discard",
            ),
            list_head=_LIST_HEAD,
            reviewed_rows=rows(reviewed, "reviewed"),
            discarded_section=discarded_section,
        )

    def review_html(self, key: str) -> Optional[str]:
        app = self.storage.get_application(key)
        if app is None:
            return None
        entries = entry_options(self.resume, app)

        blocks = []
        for entry in entries:
            items = []
            for b in entry.bullets:
                checked = "checked" if b.recommended else ""
                badge = '<span class="rec">AI pick</span>' if b.recommended else ""
                items.append(
                    f'<label class="bullet"><input type="checkbox" value="{html.escape(b.id)}" '
                    f"{checked}><span>{html.escape(b.text)}</span>{badge}</label>"
                )
            subtitle = f'<span class="dates">{html.escape(entry.subtitle)}</span>' if entry.subtitle else ""
            blocks.append(
                f'<section class="entry"><h3>{html.escape(entry.title)} {subtitle}</h3>'
                + "".join(items)
                + "</section>"
            )

        status_note = ""
        if app.status == "reviewed":
            status_note = (
                '<p class="note">Already reviewed — submitting again re-renders and '
                "re-syncs this application.</p>"
            )
        elif app.status == "withdrawn":
            status_note = (
                '<p class="note">Discarded from the pending list — submitting anyway '
                "reviews it and puts it back on the sheet.</p>"
            )
        return _REVIEW_TEMPLATE.format(
            css=_CSS,
            key=html.escape(key),
            company=html.escape(app.company_name),
            title=html.escape(app.title),
            url=html.escape(app.url),
            fit=f"{app.fit_score:.2f}",
            keywords=html.escape(", ".join(app.keywords)),  # full extracted list — this is the editing view
            full_page_bullets=_FULL_PAGE_BULLETS,
            status_note=status_note,
            entries="".join(blocks),
        )


class _Handler(BaseHTTPRequestHandler):
    app: ReviewApp  # set by build_server

    # --- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # route to structured logging
        log.debug("http " + fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, markup: str, status: int = 200) -> None:
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # --- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
        path = urlsplit(self.path).path
        if path == "/":
            self._html(self.app.index_html())
            return
        if path.startswith("/review/"):
            markup = self.app.review_html(path.removeprefix("/review/"))
            if markup is None:
                self._html("<h1>Unknown application</h1>", status=404)
            else:
                self._html(markup)
            return
        if path.startswith("/preview/") and path.endswith(".pdf"):
            key = path.removeprefix("/preview/").removesuffix(".pdf")
            base = Path(self.app.preview_dir).resolve()
            pdf = (base / f"{key}.pdf").resolve()
            # The parent check keeps a crafted key ("../...") inside the preview dir.
            if pdf.is_file() and pdf.parent == base:
                self._send(200, pdf.read_bytes(), "application/pdf")
            else:
                self._send(404, b"no preview yet", "text/plain")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 (stdlib API name)
        path = urlsplit(self.path).path
        data = self._read_json()
        key = str(data.get("key", ""))
        ids = [str(i) for i in data.get("ids", []) if isinstance(i, (str, int))]
        # discard/restore act on a batch; a lone "key" still works (one-item batch).
        keys = [str(k) for k in data.get("keys", []) if isinstance(k, (str, int))] or (
            [key] if key else []
        )
        if path == "/api/preview":
            self._json(self.app.preview(key, ids))
            return
        if path == "/api/submit":
            self._json(self.app.submit(key, ids))
            return
        if path == "/api/discard":
            self._json(self.app.discard(keys))
            return
        if path == "/api/restore":
            self._json(self.app.restore(keys))
            return
        self._json({"error": "not found"}, status=404)


def build_server(settings: Settings, storage, resume: MasterResume) -> HTTPServer:
    handler = type("BoundHandler", (_Handler,), {"app": ReviewApp(settings, storage, resume)})
    return HTTPServer(("127.0.0.1", settings.review_port), handler)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from ..config import get_settings
    from ..logging_config import configure_logging

    parser = argparse.ArgumentParser(description="Review tailored CVs before they hit the sheet.")
    parser.add_argument("--port", type=int, default=None, help="override REVIEW_PORT")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    if args.port is not None:
        settings = settings.model_copy(update={"review_port": args.port})

    try:
        resume = load_master_resume(settings.master_resume_file)
    except FileNotFoundError:
        print(f"master résumé not found at {settings.master_resume_file} — nothing to review.")
        return 1

    if find_latex_engine() is None:
        print(
            "WARNING: no LaTeX engine on PATH (tectonic/xelatex/pdflatex) — previews "
            "and submits will skip the PDF. Install tectonic: brew install tectonic"
        )

    storage = get_storage(settings)
    server = build_server(settings, storage, resume)
    url = f"http://127.0.0.1:{settings.review_port}/"
    print(f"CV review app: {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
        storage.close()
    return 0


# --- templates ---------------------------------------------------------------

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; background: #f6f7f9; color: #1c2733; }
header.top { background: #fff; border-bottom: 1px solid #e3e7ec; padding: 14px 24px; }
header.top h1 { font-size: 18px; margin: 0; }
header.top a { color: #1e64b4; text-decoration: none; }
main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e3e7ec; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #eef1f4; }
th { background: #fafbfc; font-size: 13px; text-transform: uppercase; letter-spacing: .04em; }
td.kw { color: #5b6b7b; font-size: 13px; }
td.empty { color: #8a97a5; font-style: italic; }
a.btn, button { display: inline-block; background: #1e64b4; color: #fff; border: 0;
  border-radius: 6px; padding: 7px 14px; font-size: 14px; cursor: pointer;
  text-decoration: none; }
button.secondary { background: #eef1f4; color: #1c2733; }
button.danger { background: #fff; color: #c0392b; border: 1px solid #e6c3bd; }
button.danger:hover:enabled { background: #fdf0ee; }
button:disabled { opacity: .5; cursor: default; }
td.act { white-space: nowrap; }
/* Bulk picking: checkboxes and the confirm/cancel pair only exist once the human
   arms the bar, so an unarmed list looks exactly like a plain table. */
.bar { display: flex; gap: 10px; align-items: center; margin: 0 0 8px; }
.bar .hint { color: #8a97a5; font-size: 13px; }
.bar .picking-only { display: none; gap: 10px; align-items: center; }
section.list.picking .bar .picking-only { display: flex; }
section.list.picking .bar .startpick, section.list.picking .rowbtn { display: none; }
section.list.picking .bar { position: sticky; top: 0; z-index: 2; background: #f6f7f9;
  padding: 8px 0; border-bottom: 1px solid #e3e7ec; }
th.pick, td.pick { display: none; width: 34px; }
section.list.picking th.pick, section.list.picking td.pick { display: table-cell; }
input.rowpick, input.allpick { width: 16px; height: 16px; cursor: pointer; }
h2 { font-size: 15px; margin: 22px 0 8px; }
.cols { display: flex; gap: 20px; align-items: flex-start; }
.cols form { flex: 1 1 46%; min-width: 380px; }
.cols aside { flex: 1 1 54%; position: sticky; top: 16px; }
section.entry { background: #fff; border: 1px solid #e3e7ec; border-radius: 8px;
  padding: 12px 14px; margin-bottom: 12px; }
section.entry h3 { margin: 0 0 6px; font-size: 14.5px; }
span.dates { color: #8a97a5; font-weight: normal; font-size: 12.5px; margin-left: 6px; }
label.bullet { display: flex; gap: 9px; padding: 6px 4px; border-radius: 6px;
  align-items: flex-start; cursor: pointer; }
label.bullet:hover { background: #f2f6fb; }
label.bullet input { margin-top: 3px; }
label.bullet span { flex: 1; }
span.rec { background: #e3f0ff; color: #1e64b4; font-size: 11px; padding: 1px 7px;
  border-radius: 9px; white-space: nowrap; margin-top: 2px; }
.meta { background: #fff; border: 1px solid #e3e7ec; border-radius: 8px;
  padding: 10px 14px; margin-bottom: 10px; display: flex; gap: 14px;
  align-items: center; flex-wrap: wrap; }
#pagebadge { font-weight: 600; }
#pagebadge.ok { color: #1a7f37; }
#pagebadge.over { color: #c0392b; }
iframe { width: 100%; height: 78vh; border: 1px solid #e3e7ec; border-radius: 8px;
  background: #fff; }
p.note { color: #8a5b00; background: #fff7e0; border: 1px solid #f0e0ae;
  padding: 8px 12px; border-radius: 6px; }
#result { margin-top: 10px; }
#result a { color: #1e64b4; }
.chips { color: #5b6b7b; font-size: 13px; }
"""

_LIST_HEAD = "<th>Company</th><th>Role</th><th>Fit</th><th>Keywords</th><th></th>"

# The bulk bar for one list. Collapsed to a single "… several…" button until the
# human arms it (see the .picking CSS), so the page reads the same as before.
_PICK_BAR = """<div class="bar">
  <button class="danger startpick" onclick="startPick('{mode}')">{start}</button>
  <span class="picking-only">
    <button class="danger" id="{mode}-apply" onclick="applyPick('{mode}')">{verb} 0</button>
    <button class="secondary" onclick="cancelPick('{mode}')">Cancel</button>
    <span class="hint">{hint}</span>
  </span>
</div>"""

_LIST_SECTION = """<section class="list" id="sec-{mode}">
<h2>{heading}</h2>
{bar}
<table><tr>{head}</tr>
{rows}</table>
</section>"""

_INDEX_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CV Review</title><style>{css}</style></head>
<body>
<header class="top"><h1>CV Review — {pending_count} pending · {reviewed_count} reviewed</h1></header>
<main>
{pending_section}
<h2>Reviewed (already on the sheet)</h2>
<table><tr>{list_head}</tr>
{reviewed_rows}</table>
{discarded_section}
<script>
const VERB = {{pending: 'Discard', discarded: 'Restore'}};
const ENDPOINT = {{pending: '/api/discard', discarded: '/api/restore'}};
function sec(mode) {{ return document.getElementById('sec-' + mode); }}
function boxes(mode) {{ return Array.from(sec(mode).querySelectorAll('input.rowpick')); }}
function picked(mode) {{ return boxes(mode).filter(b => b.checked); }}
function updatePick(mode) {{
  const all = boxes(mode), n = picked(mode).length;
  const apply = document.getElementById(mode + '-apply');
  apply.textContent = VERB[mode] + ' ' + n;
  apply.disabled = n === 0;
  const master = sec(mode).querySelector('input.allpick');
  master.checked = n > 0 && n === all.length;
  master.indeterminate = n > 0 && n < all.length;
}}
function startPick(mode, key) {{
  sec(mode).classList.add('picking');
  if (key) {{
    const box = boxes(mode).find(b => b.value === key);
    if (box) box.checked = true;
  }}
  updatePick(mode);
}}
function cancelPick(mode) {{
  boxes(mode).forEach(b => {{ b.checked = false; }});
  sec(mode).classList.remove('picking');
  updatePick(mode);
}}
function toggleAll(mode, el) {{
  boxes(mode).forEach(b => {{ b.checked = el.checked; }});
  updatePick(mode);
}}
async function applyPick(mode) {{
  const keys = picked(mode).map(b => b.value);
  if (!keys.length) return;
  if (mode === 'pending' && !confirm('Discard ' + keys.length + ' job(s)? They never reach '
      + 'the sheet, and stay restorable in the discarded list.')) return;
  const btn = document.getElementById(mode + '-apply');
  btn.disabled = true; btn.textContent = 'Working\\u2026';
  const resp = await fetch(ENDPOINT[mode], {{method: 'POST',
    headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{keys: keys}})}});
  const data = await resp.json();
  if (data.errors && data.errors.length) alert(data.errors.join('\\n\\n'));
  location.reload();
}}
</script>
</main>
</body></html>
"""

_REVIEW_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{company} — CV review</title><style>{css}</style></head>
<body>
<header class="top">
  <h1><a href="/">← All applications</a> &nbsp;|&nbsp; {company} —
      <a href="{url}" target="_blank" rel="noopener">{title}</a></h1>
  <div class="chips">fit {fit} &nbsp;·&nbsp; keywords: {keywords}</div>
</header>
<main>
{status_note}
<p>Header, education and skills are included automatically. Pick the experience and
project bullets to keep — the <b>AI's picks are prechecked</b>. Aim for a full page
(~{full_page_bullets} bullets); the preview shows the real page count.</p>
<div class="cols">
  <form id="sel" onsubmit="return false">{entries}</form>
  <aside>
    <div class="meta">
      <span id="count"></span>
      <span id="pagebadge">no preview yet</span>
      <button id="previewbtn" onclick="preview()">Update preview</button>
      <button id="submitbtn" onclick="submitCv()">Submit → sheet</button>
    </div>
    <div id="result"></div>
    <iframe id="pdf" title="CV preview"></iframe>
  </aside>
</div>
<script>
const KEY = "{key}";
function ids() {{
  return Array.from(document.querySelectorAll('#sel input:checked')).map(i => i.value);
}}
function setBadge(data) {{
  const el = document.getElementById('pagebadge');
  document.getElementById('count').textContent = data.bullets + ' bullets selected';
  if (!data.pdf) {{
    el.className = 'over';
    el.textContent = data.engine ? 'PDF failed' : 'no LaTeX engine (brew install tectonic)';
  }} else if (data.pages === 1) {{
    el.className = 'ok'; el.textContent = '1 page \\u2713';
  }} else if (data.pages > 1) {{
    el.className = 'over'; el.textContent = data.pages + ' pages \\u2014 uncheck something';
  }} else {{
    el.className = ''; el.textContent = 'pages unknown';
  }}
}}
async function post(path) {{
  const resp = await fetch(path, {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key: KEY, ids: ids()}})}});
  return resp.json();
}}
async function preview() {{
  const b = document.getElementById('previewbtn');
  b.disabled = true; b.textContent = 'Rendering\\u2026';
  try {{
    const data = await post('/api/preview');
    if (data.error) {{ document.getElementById('result').textContent = data.error; return; }}
    setBadge(data);
    if (data.pdf) document.getElementById('pdf').src = '/preview/' + KEY + '.pdf?t=' + Date.now();
  }} finally {{ b.disabled = false; b.textContent = 'Update preview'; }}
}}
async function submitCv() {{
  const badge = document.getElementById('pagebadge');
  if (badge.classList.contains('over') &&
      !confirm('The preview did not fit one page. Submit anyway?')) return;
  const b = document.getElementById('submitbtn');
  b.disabled = true; b.textContent = 'Submitting\\u2026';
  try {{
    const data = await post('/api/submit');
    const out = document.getElementById('result');
    if (data.error) {{ out.textContent = data.error; return; }}
    let bits = ['<b>Submitted.</b>'];
    if (data.drive_link) bits.push('<a href="' + data.drive_link + '" target="_blank">Drive CV</a>');
    if (data.sheet_synced) bits.push('row synced to the sheet \\u2713');
    else bits.push('sheet not synced' + (data.sheet_error ? ' (' + data.sheet_error + ')'
                   : ' (tracker not configured \\u2014 the daily run will sync it)'));
    out.innerHTML = bits.join(' &nbsp;\\u00b7&nbsp; ') + ' &nbsp;<a href="/">Next \\u2192</a>';
  }} finally {{ b.disabled = false; b.textContent = 'Submit \\u2192 sheet'; }}
}}
window.addEventListener('load', preview);
</script>
</main>
</body></html>
"""

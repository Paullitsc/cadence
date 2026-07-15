"""The review app's HTTP server (stdlib only — no new runtime dependency).

Single-threaded on purpose: requests are quick page builds or one LaTeX compile,
the app is single-user local tooling, and serializing requests keeps SQLite and
the preview files race-free. Endpoints:

    GET  /                    pending + reviewed application lists
    GET  /review/<key>        the selection UI for one application
    POST /api/preview         {key, ids} → compile the selection, report pages
    GET  /preview/<key>.pdf   the last compiled preview for that application
    POST /api/submit          {key, ids} → finalize: render, Drive upload,
                              storage update (status → "reviewed"), sheet row

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

    # --- page builds --------------------------------------------------------

    def index_html(self) -> str:
        pending = self.storage.list_applications(status="pending_review")
        reviewed = self.storage.list_applications(status="reviewed")

        def rows(apps: list[Application], action: str) -> str:
            if not apps:
                return '<tr><td colspan="5" class="empty">none</td></tr>'
            out = []
            for a in apps:
                # Full extracted keyword list (~20): this is the human's triage
                # view, and seeing what the JD wants is how they pick what to
                # customize — truncating it hid the signal.
                kw = ", ".join(a.keywords)
                out.append(
                    "<tr>"
                    f"<td>{html.escape(a.company_name)}</td>"
                    f'<td><a href="{html.escape(a.url)}" target="_blank" rel="noopener">'
                    f"{html.escape(a.title)}</a></td>"
                    f"<td>{a.fit_score:.2f}</td>"
                    f"<td class=kw>{html.escape(kw)}</td>"
                    f'<td><a class="btn" href="/review/{html.escape(a.dedupe_key)}">{action}</a></td>'
                    "</tr>"
                )
            return "".join(out)

        return _INDEX_TEMPLATE.format(
            css=_CSS,
            pending_count=len(pending),
            reviewed_count=len(reviewed),
            pending_rows=rows(pending, "Review"),
            reviewed_rows=rows(reviewed, "Reopen"),
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
        if path == "/api/preview":
            self._json(self.app.preview(key, ids))
            return
        if path == "/api/submit":
            self._json(self.app.submit(key, ids))
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
button:disabled { opacity: .5; cursor: default; }
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

_INDEX_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CV Review</title><style>{css}</style></head>
<body>
<header class="top"><h1>CV Review — {pending_count} pending · {reviewed_count} reviewed</h1></header>
<main>
<h2>Pending review</h2>
<table><tr><th>Company</th><th>Role</th><th>Fit</th><th>Keywords</th><th></th></tr>
{pending_rows}</table>
<h2>Reviewed (already on the sheet)</h2>
<table><tr><th>Company</th><th>Role</th><th>Fit</th><th>Keywords</th><th></th></tr>
{reviewed_rows}</table>
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

"""LaTeX CV generation: escaping, Markdown conversion, Resume.tex structure,
engine fallback, and the fit-to-one-page trim loop."""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.resume.latex import (
    build_latex,
    compile_latex,
    format_date,
    format_date_range,
    latex_escape,
    latex_escape_url,
    md_to_latex,
    pdf_page_count,
)
from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.render import (
    build_cv_doc,
    write_and_render_one_page,
)
from internship_pipeline.resume.tailoring import TailoredBullet

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


def _tailored(resume, limit=None):
    bullets = [TailoredBullet(ref=b, text=b.text) for b in all_bullets(resume)]
    return bullets[:limit] if limit else bullets


# --- escaping / markdown --------------------------------------------------------
def test_latex_escape_neutralizes_specials():
    out = latex_escape(r"100% of A&B_c #1 {x} $5 ~ ^ \evil")
    for ch in ("%", "&", "_", "#", "$"):
        assert f"\\{ch}" in out
    assert "\\textbackslash{}evil" in out  # backslash neutralized, braces intact
    assert "\\textasciitilde{}" in out and "\\textasciicircum{}" in out
    # no raw specials survive
    assert "\\evil" not in out


def test_latex_escape_renders_ascii_arrow_as_rightarrow():
    assert latex_escape("S3 -> Glue -> Redshift") == r"S3 $\rightarrow$ Glue $\rightarrow$ Redshift"
    assert "<" not in latex_escape("under <2 cm")  # \textless{}, not a raw <


def test_md_to_latex_bold_and_links():
    assert md_to_latex("uses **GraphQL** daily") == r"uses \textbf{GraphQL} daily"
    out = md_to_latex("see [demo](https://a.dev/x_y) now")
    assert out == r"see \href{https://a.dev/x\_y}{demo} now"
    # bold inside link text
    assert md_to_latex("[**src**](https://b.dev)") == r"\href{https://b.dev}{\textbf{src}}"


def test_latex_escape_url_escapes_tex_specials():
    assert latex_escape_url("https://x.dev/a_b#c%20d") == r"https://x.dev/a\_b\#c\%20d"


# --- dates -----------------------------------------------------------------------
def test_format_date_and_range():
    assert format_date("2025-01") == "Jan 2025"
    assert format_date("present") == "Present"
    assert format_date(None) == ""
    assert format_date("Fall 2025") == "Fall 2025"  # unknown format passes through
    assert format_date_range("2025-01", "2025-04") == "Jan 2025 -- Apr 2025"
    assert format_date_range("2025-11", "present") == "Nov 2025 -- Present"
    assert format_date_range(None, "2025-04") == "Apr 2025"


# --- document structure (Resume.tex style) ---------------------------------------
def test_build_latex_replicates_resume_tex_structure():
    resume = load_master_resume(FIXTURE)
    tex = build_latex(build_cv_doc(resume, _tailored(resume)))

    # template preamble + macros
    assert r"\documentclass[a4paper,9pt]{extarticle}" in tex
    assert r"\newcommand{\repolink}" in tex and r"\newcommand{\sqbullet}" in tex
    # section order: EXPERIENCE -> EDUCATION -> SKILLS -> PROJECTS
    order = [tex.index(f"\\section*{{{name}}}") for name in
             ("PROFESSIONAL EXPERIENCE", "EDUCATION", "TECHNICAL SKILLS", "PROJECTS")]
    assert order == sorted(order)
    # linked project renders as a blue repolink and shows the note line
    assert r"\repolink{https://github.com/testcand/query-planner}{Query Planner}" in tex
    assert "Blue titles link to source repositories" in tex
    # header carries the candidate contact line
    assert "Test Candidate" in tex
    assert tex.strip().endswith(r"\end{document}")


def test_build_latex_without_project_links_omits_note_line():
    resume = load_master_resume(FIXTURE)
    for proj in resume.projects:
        proj.url = None
    tex = build_latex(build_cv_doc(resume, _tailored(resume)))
    assert "Blue titles link" not in tex
    assert r"\repolink" not in tex.split(r"\begin{document}")[1]


def test_build_latex_only_selected_entries_render():
    resume = load_master_resume(FIXTURE)
    only_first = _tailored(resume, limit=1)  # one experience bullet
    tex = build_latex(build_cv_doc(resume, only_first))
    assert "PROJECTS" not in tex  # no project contributed a bullet
    assert "Acme Labs" in tex


# --- compile fallback -------------------------------------------------------------
def test_compile_latex_skips_without_engine(tmp_path):
    # conftest hides any real engine from the suite
    tex = tmp_path / "cv.tex"
    tex.write_text("\\documentclass{article}\\begin{document}x\\end{document}")
    assert compile_latex(str(tex)) is None


def test_pdf_page_count_regex_fallback_and_unreadable(tmp_path, monkeypatch):
    # force the regex fallback even when pypdf is installed
    import builtins

    import internship_pipeline.resume.latex as latex
    real_import = builtins.__import__

    def no_pypdf(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("hidden for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.5\n1 0 obj\n<< /Type /Pages /Kids [] /Count 2 >>\nendobj")
    assert latex.pdf_page_count(str(pdf)) == 2
    opaque = tmp_path / "y.pdf"
    opaque.write_bytes(b"%PDF-1.5 compressed-object-streams-only")
    assert latex.pdf_page_count(str(opaque)) is None
    assert pdf_page_count(str(tmp_path / "missing.pdf")) is None


# --- one-page fit trim -------------------------------------------------------------
def _fake_engine(monkeypatch, tmp_pages: dict):
    """Fake LaTeX toolchain: compiles instantly, page count = f(bullet count)."""
    import internship_pipeline.resume.latex as latex
    import internship_pipeline.resume.render as render

    def fake_which(name):
        return "/usr/bin/faketex"

    def fake_run(cmd, cwd=None, **kwargs):
        (Path(cwd) / cmd[-1]).with_suffix(".pdf").write_bytes(b"%PDF")

        class P:
            returncode = 0
            stderr = ""
            stdout = ""
        return P()

    monkeypatch.setattr(latex.shutil, "which", fake_which)
    monkeypatch.setattr(latex.subprocess, "run", fake_run)
    monkeypatch.setattr(render, "find_latex_engine", lambda: "faketex")

    def fake_pages(pdf_path):
        return tmp_pages["pages_fn"](pdf_path)

    monkeypatch.setattr(render, "pdf_page_count", fake_pages)


def _padded_resume():
    """The fixture résumé with extra bullets, so there is room to trim."""
    from internship_pipeline.resume.models import Bullet

    resume = load_master_resume(FIXTURE)
    resume.experiences[0].bullets.extend(
        Bullet(text=f"Extra accomplishment number {i} in Python.") for i in range(6)
    )
    return resume


def test_one_page_render_trims_least_relevant_last(tmp_path, monkeypatch):
    resume = _padded_resume()
    bullets = _tailored(resume)
    assert len(bullets) >= 5

    state = {"renders": 0}

    def pages_fn(_pdf):
        # every render re-reads the current bullet count via closure over `state`
        return 1 if state["kept"] <= len(bullets) - 2 else 2

    def counting_pages(_pdf):
        state["renders"] += 1
        return pages_fn(_pdf)

    # track how many bullets the most recent doc carried
    import internship_pipeline.resume.render as render
    real_build = render.build_cv_doc

    def tracking_build(res, tb):
        state["kept"] = len(tb)
        return real_build(res, tb)

    state["kept"] = len(bullets)
    monkeypatch.setattr(render, "build_cv_doc", tracking_build)
    _fake_engine(monkeypatch, {"pages_fn": counting_pages})

    result = write_and_render_one_page(resume, bullets, str(tmp_path), "fit")
    assert result.dropped == 2
    assert len(result.bullets) == len(bullets) - 2
    # dropped from the END (least relevant last)
    assert [tb.ref.id for tb in result.bullets] == [tb.ref.id for tb in bullets[:-2]]
    assert result.pages == 1
    assert result.pdf_path and result.pdf_path.endswith("fit.pdf")


def test_one_page_render_never_trims_below_floor(tmp_path, monkeypatch):
    resume = _padded_resume()
    bullets = _tailored(resume)

    _fake_engine(monkeypatch, {"pages_fn": lambda _pdf: 2})  # never fits

    result = write_and_render_one_page(resume, bullets, str(tmp_path), "floor")
    from internship_pipeline.resume.render import _MIN_BULLETS

    assert len(result.bullets) == _MIN_BULLETS
    assert result.pages == 2  # honest: it still doesn't fit


def test_one_page_render_without_engine_returns_untrimmed(tmp_path):
    resume = load_master_resume(FIXTURE)
    bullets = _tailored(resume)
    result = write_and_render_one_page(resume, bullets, str(tmp_path), "noengine")
    assert result.pdf_path is None
    assert result.dropped == 0
    assert len(result.bullets) == len(bullets)
    assert Path(result.yaml_path).exists()

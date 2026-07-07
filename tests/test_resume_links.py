"""Résumé links: project names become Markdown links, bullet links survive tailoring,
and the render path pins one PDF per job (no cross-job overwrite).
"""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.rendercv import build_rendercv_cv, write_and_render
from internship_pipeline.resume.tailoring import (
    TailoredBullet,
    enforce_grounding,
    markdown_link_urls,
    tailor_resume,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


def _tailored(resume):
    return [TailoredBullet(ref=b, text=b.text) for b in all_bullets(resume)]


# --- markdown link extraction -------------------------------------------------- #
def test_markdown_link_urls_extracts_targets():
    assert markdown_link_urls("See [demo](https://a.com/d) and [src](https://b.com).") == {
        "https://a.com/d", "https://b.com",
    }
    assert markdown_link_urls("no links here") == set()


# --- project names render as links --------------------------------------------- #
def test_project_with_url_renders_name_as_markdown_link():
    resume = load_master_resume(FIXTURE)
    doc = build_rendercv_cv(resume, _tailored(resume))
    projects = doc["cv"]["sections"]["projects"]
    # fixture project has url -> name becomes [name](url); no ignored bare `url:` key
    assert projects[0]["name"] == "[Query Planner](https://github.com/testcand/query-planner)"
    assert "url" not in projects[0]


def test_project_without_url_keeps_plain_name():
    resume = load_master_resume(FIXTURE)
    resume.projects[0].url = None
    doc = build_rendercv_cv(resume, _tailored(resume))
    assert doc["cv"]["sections"]["projects"][0]["name"] == "Query Planner"


def test_two_roles_at_same_company_keep_separate_highlights():
    # Two Experience entries sharing a company name (a promotion, a return
    # internship, etc.) must not merge their bullets — each entry's highlights
    # come only from ITS OWN bullets.
    resume = load_master_resume(FIXTURE)
    second_role = resume.experiences[0].model_copy(
        update={
            "role": "Senior Software Engineer Intern",
            "start_date": "2026-05",
            "end_date": "2026-08",
            "bullets": [
                b.model_copy(update={"text": f"Second role: {b.text}"})
                for b in resume.experiences[0].bullets
            ],
        }
    )
    resume.experiences.append(second_role)

    doc = build_rendercv_cv(resume, _tailored(resume))
    entries = doc["cv"]["sections"]["experience"]
    assert len(entries) == 2
    first_highlights = entries[0]["highlights"]
    second_highlights = entries[1]["highlights"]
    assert all("Second role:" not in h for h in first_highlights)
    assert all("Second role:" in h for h in second_highlights)


# --- links survive tailoring ---------------------------------------------------- #
def test_grounding_rejects_rephrase_that_drops_or_alters_a_link():
    orig = "Built a [pipeline](https://github.com/x/pipe) in Python."
    vocab = {"built", "pipeline", "python", "github.com", "x", "pipe", "https", "kafka"}
    # dropped the link -> verbatim fallback
    assert enforce_grounding("Built a pipeline in Python.", orig, vocab) == orig
    # kept the link exactly -> rephrase accepted
    kept = "Built a Python [pipeline](https://github.com/x/pipe)."
    assert enforce_grounding(kept, orig, vocab) == kept
    # invented a link the original didn't have -> verbatim fallback
    assert enforce_grounding("Built a [pipeline](https://evil.com) in Python.", orig, vocab) == orig


def test_llm_mangled_link_falls_back_verbatim_end_to_end():
    resume = load_master_resume(FIXTURE)
    bullets = all_bullets(resume)
    linked = bullets[0].model_copy(
        update={"text": f"{bullets[0].text} ([code](https://github.com/testcand/pipe))"}
    )
    bullets = [linked, *bullets[1:]]

    def fake_complete(system_blocks, user_text):
        # rephrases but silently drops the markdown link
        return {"selected": [{"id": linked.id, "text": linked.text.split(" ([code]")[0]}]}

    result = tailor_resume(
        jd_text="Backend data pipelines in Python.", keywords=["python"],
        candidate_bullets=bullets, resume=resume, complete=fake_complete,
    )
    # verbatim words + intact link; keyword bolding may add ** but never words
    assert result.bullets[0].text.replace("**", "") == linked.text
    assert "[code](https://github.com/testcand/pipe)" in result.bullets[0].text


# --- one PDF per job (no overwrite) --------------------------------------------- #
def test_write_and_render_pins_pdf_per_slug(tmp_path, monkeypatch):
    import internship_pipeline.resume.rendercv as rc

    rendered_cmds = []

    def fake_run(cmd, **kwargs):
        rendered_cmds.append(cmd)
        # simulate rendercv writing the pinned pdf next to the yaml
        (tmp_path / cmd[cmd.index("--pdf-path") + 1]).write_bytes(b"%PDF")

        class P:
            returncode = 0
            stderr = ""
        return P()

    monkeypatch.setattr(rc.shutil, "which", lambda _: "/usr/bin/rendercv")
    monkeypatch.setattr(rc.subprocess, "run", fake_run)

    resume = load_master_resume(FIXTURE)
    doc = build_rendercv_cv(resume, _tailored(resume))
    _, pdf_a = write_and_render(doc, str(tmp_path), "job-a")
    _, pdf_b = write_and_render(doc, str(tmp_path), "job-b")

    assert pdf_a.endswith("job-a.pdf") and pdf_b.endswith("job-b.pdf")
    assert pdf_a != pdf_b
    assert "--pdf-path" in rendered_cmds[0]

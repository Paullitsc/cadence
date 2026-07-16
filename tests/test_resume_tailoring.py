from __future__ import annotations

from pathlib import Path

from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.matching import content_tokens, extract_keywords
from internship_pipeline.resume.render import build_cv_doc, to_yaml, write_and_render
from internship_pipeline.resume.tailoring import bold_keywords, enforce_grounding, tailor_resume

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")

JD = "Backend internship building data pipelines in Python and Kafka."


def _setup():
    resume = load_master_resume(FIXTURE)
    bullets = all_bullets(resume)
    keywords = extract_keywords(JD, resume=resume)
    return resume, bullets, keywords


def _input_vocab(bullets, keywords):
    vocab = set(content_tokens(JD))
    for kw in keywords:
        vocab |= content_tokens(kw)
    for b in bullets:
        vocab |= content_tokens(b.searchable_text())
    return vocab


def test_enforce_grounding_rejects_ungrounded_and_keeps_grounded():
    vocab = {"python", "kafka", "pipeline", "built"}
    # grounded rephrase (subset of vocab) passes through
    assert enforce_grounding("Built Python pipeline", "orig", vocab) == "Built Python pipeline"
    # ungrounded (introduces "rust") -> falls back to the verbatim original
    assert enforce_grounding("Built Rust pipeline", "orig", vocab) == "orig"


def test_deterministic_select_only_keeps_bullet_words_verbatim():
    """Words never change in select-only mode; keyword bolding only adds ``**``."""
    resume, bullets, keywords = _setup()
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets,
        resume=resume, complete=None, max_bullets=10,
    )
    assert result.used_llm is False
    assert [tb.text.replace("**", "") for tb in result.bullets] == [b.text for b in bullets]
    # JD keywords present in the bullets came back bolded.
    assert any("**" in tb.text for tb in result.bullets)


def test_bold_keywords_basic_and_case_preserving():
    assert bold_keywords("Built Python pipelines", ["python"]) == "Built **Python** pipelines"
    # multi-word keyword wins over its parts (longest first)
    out = bold_keywords("Applied machine learning models", ["learning", "machine learning"])
    assert out == "Applied **machine learning** models"


def test_bold_keywords_never_double_bolds_or_touches_links():
    assert bold_keywords("Used **Python** daily", ["python"]) == "Used **Python** daily"
    text = "See [python demo](https://x.dev/python) for details"
    assert bold_keywords(text, ["python"]) == text  # link text/URL untouched
    # no partial-word matches ("java" inside "javascript")
    assert bold_keywords("Wrote JavaScript apps", ["java"]) == "Wrote JavaScript apps"


def test_bold_keywords_handles_tech_punctuation():
    assert bold_keywords("Shipped C++ services", ["c++"]) == "Shipped **C++** services"
    assert bold_keywords("No match here", []) == "No match here"


def test_emphasize_restores_master_bold_dropped_by_rephrase():
    """LLM rephrases that keep the words but drop ``**`` get master emphasis back."""
    from internship_pipeline.resume.tailoring import emphasize

    master = "Developed **5+ GraphQL APIs** using **SQL** and **RESTful API** design."
    rephrased = "Developed 5+ GraphQL APIs using SQL and RESTful API design."
    out = emphasize(rephrased, master_text=master, keywords=["graphql"])
    assert "**5+ GraphQL APIs**" in out
    assert "**SQL**" in out
    assert "**RESTful API**" in out


def test_tailoring_never_introduces_ungrounded_tokens():
    """Anti-hallucination regression: fabricated LLM output must not reach the résumé."""
    resume, bullets, keywords = _setup()
    vocab = _input_vocab(bullets, keywords)

    # The mocked LLM tries to fabricate a metric, an employer, and skills.
    def fake_complete(system_blocks, user_text):
        return {
            "selected": [
                {"id": bullets[0].id, "text": "Increased revenue by 400% at Google using Rust."},
                {"id": bullets[1].id, "text": bullets[1].text},  # legitimate verbatim
            ],
            "human_review": True,
        }

    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets,
        resume=resume, complete=fake_complete, max_bullets=10,
    )

    # Every content token in the output is present in the tailoring input.
    out_tokens: set[str] = set()
    for tb in result.bullets:
        out_tokens |= content_tokens(tb.text)
    assert out_tokens <= vocab

    # The specific fabricated tokens never appear.
    for forbidden in ("revenue", "400", "google", "rust"):
        assert forbidden not in out_tokens

    # The fabricated bullet fell back to its verbatim original; the honest one passed.
    # (Keyword bolding may add ``**`` but never changes the words.)
    assert result.bullets[0].text.replace("**", "") == bullets[0].text
    assert result.bullets[1].text.replace("**", "") == bullets[1].text
    assert result.human_review is True


def test_tailoring_falls_back_verbatim_when_llm_call_raises():
    """A failed LLM call (truncated JSON, API error) degrades to select-only for
    that one CV instead of propagating and killing the whole stage."""
    resume, bullets, keywords = _setup()

    def boom(system_blocks, user_text):
        raise ValueError("no JSON object found in model response")

    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets,
        resume=resume, complete=boom, max_bullets=10,
    )
    assert result.used_llm is False
    assert [tb.text.replace("**", "") for tb in result.bullets] == [b.text for b in bullets]


def test_tailoring_drops_unknown_ids_from_llm():
    resume, bullets, keywords = _setup()

    def fake_complete(system_blocks, user_text):
        return {"selected": [
            {"id": "does-not-exist", "text": "ghost bullet"},
            {"id": bullets[0].id, "text": bullets[0].text},
        ]}

    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets,
        resume=resume, complete=fake_complete,
    )
    assert [tb.ref.id for tb in result.bullets] == [bullets[0].id]


def test_build_cv_doc_shape_and_yaml():
    resume, bullets, keywords = _setup()
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets, resume=resume, complete=None,
    )
    cv_doc = build_cv_doc(resume, result.bullets)
    assert cv_doc["cv"]["name"] == "Test Candidate"
    sections = cv_doc["cv"]["sections"]
    # experience entries carry the tailored bullets as highlights
    exp = sections["experience"][0]
    assert exp["company"] == "Acme Labs"
    assert exp["position"] == "Software Engineer Intern"
    assert any("Kafka" in h for h in exp["highlights"])
    assert sections["skills"][0] == {"label": "Languages", "details": "Python, Java, SQL"}
    # serializes to valid YAML
    text = to_yaml(cv_doc)
    assert "Test Candidate" in text


def test_build_cv_doc_citizenship_line_is_conditional():
    resume, bullets, keywords = _setup()
    resume = resume.model_copy(update={
        "citizenship": "US Citizen", "citizenship_canada": "US and Canadian Citizen",
    })
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets, resume=resume, complete=None,
    )
    default_summary = build_cv_doc(resume, result.bullets)["cv"]["sections"]["summary"][0]
    assert default_summary.endswith("US Citizen.")
    assert "Canadian" not in default_summary

    canada_summary = build_cv_doc(resume, result.bullets, is_canadian=True)["cv"]["sections"]["summary"][0]
    assert canada_summary.endswith("US and Canadian Citizen.")


def test_build_cv_doc_canadian_falls_back_to_default_citizenship_when_unset():
    resume, bullets, keywords = _setup()
    resume = resume.model_copy(update={"citizenship": "US Citizen", "citizenship_canada": None})
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets, resume=resume, complete=None,
    )
    summary = build_cv_doc(resume, result.bullets, is_canadian=True)["cv"]["sections"]["summary"][0]
    assert summary.endswith("US Citizen.")


def test_write_and_render_writes_yaml_and_tex_and_skips_pdf_without_engine(tmp_path, monkeypatch):
    import internship_pipeline.resume.latex as latex

    monkeypatch.setattr(latex.shutil, "which", lambda _: None)  # simulate no LaTeX engine
    resume, bullets, keywords = _setup()
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets, resume=resume, complete=None,
    )
    cv_doc = build_cv_doc(resume, result.bullets)
    yaml_path, pdf_path = write_and_render(cv_doc, str(tmp_path), "acme")
    assert Path(yaml_path).exists()
    assert (tmp_path / "acme.tex").exists()  # the .tex artifact is always written
    assert pdf_path is None

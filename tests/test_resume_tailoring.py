from __future__ import annotations

from pathlib import Path

from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.matching import content_tokens, extract_keywords
from internship_pipeline.resume.rendercv import build_rendercv_cv, to_yaml, write_and_render
from internship_pipeline.resume.tailoring import enforce_grounding, tailor_resume

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


def test_deterministic_select_only_keeps_bullets_verbatim():
    resume, bullets, keywords = _setup()
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets,
        resume=resume, complete=None, max_bullets=10,
    )
    assert result.used_llm is False
    assert [tb.text for tb in result.bullets] == [b.text for b in bullets]


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
    assert result.bullets[0].text == bullets[0].text
    assert result.bullets[1].text == bullets[1].text
    assert result.human_review is True


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


def test_build_rendercv_cv_shape_and_yaml():
    resume, bullets, keywords = _setup()
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets, resume=resume, complete=None,
    )
    cv_doc = build_rendercv_cv(resume, result.bullets)
    assert cv_doc["cv"]["name"] == "Test Candidate"
    assert cv_doc["design"]["theme"] == "classic"
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


def test_write_and_render_writes_yaml_and_skips_pdf_without_cli(tmp_path, monkeypatch):
    import internship_pipeline.resume.rendercv as rc

    monkeypatch.setattr(rc.shutil, "which", lambda _: None)  # simulate no rendercv CLI
    resume, bullets, keywords = _setup()
    result = tailor_resume(
        jd_text=JD, keywords=keywords, candidate_bullets=bullets, resume=resume, complete=None,
    )
    cv_doc = build_rendercv_cv(resume, result.bullets)
    yaml_path, pdf_path = write_and_render(cv_doc, str(tmp_path), "acme")
    assert Path(yaml_path).exists()
    assert pdf_path is None

from __future__ import annotations

from pathlib import Path

from internship_pipeline.models import Job
from internship_pipeline.resume.embeddings import HashingEmbedder, cosine
from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.matching import (
    content_tokens,
    extract_keywords,
    score_job,
    tokenize,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


def test_tokenize_preserves_tech_punctuation():
    toks = tokenize("Experienced in C++, C#, Node.js and CI-CD.")
    assert "c++" in toks
    assert "c#" in toks
    assert "node.js" in toks
    assert "ci-cd" in toks


def test_content_tokens_drop_stopwords_and_singletons():
    ct = content_tokens("We are looking for a strong Python and Kafka engineer")
    assert "python" in ct and "kafka" in ct
    assert "we" not in ct and "for" not in ct and "a" not in ct


def test_extract_keywords_prioritizes_tech_terms():
    jd = "Backend internship. Must know Python and Kafka. Nice to have Docker and SQL."
    resume = load_master_resume(FIXTURE)
    kws = extract_keywords(jd, resume=resume, top_n=8)
    assert "python" in kws
    assert "kafka" in kws
    # a stopword-y filler word should not surface
    assert "must" not in kws


def test_hashing_embedder_deterministic_and_cosine_in_unit_range():
    emb = HashingEmbedder(dim=256)
    v1 = emb.embed_one("python kafka data pipeline")
    v2 = emb.embed_one("python kafka data pipeline")
    assert v1 == v2  # deterministic
    other = emb.embed_one("graphic design illustrator")
    assert cosine(v1, v2) == 1.0
    assert 0.0 <= cosine(v1, other) < 1.0


def _score(job: Job):
    resume = load_master_resume(FIXTURE)
    bullets = all_bullets(resume)
    emb = HashingEmbedder(dim=512)
    vectors = emb.embed([b.searchable_text() for b in bullets])
    return score_job(job, bullets, vectors, emb, resume=resume, top_k=3)


def test_relevant_job_scores_higher_and_retrieves_matching_bullet():
    relevant = Job(
        company_name="DataCorp",
        title="Backend Engineering Intern",
        url="https://x/1",
        description="Build data pipelines in Python and Kafka. Backend systems.",
    )
    irrelevant = Job(
        company_name="Studio",
        title="Graphic Design Intern",
        url="https://x/2",
        description="Create marketing illustrations in Photoshop and Illustrator.",
    )
    hi = _score(relevant)
    lo = _score(irrelevant)
    assert hi.fit_score > lo.fit_score
    assert 0.0 <= hi.fit_score <= 1.0
    # the Kafka/Python bullet is the top retrieval for the backend JD
    assert "kafka" in hi.top_bullets[0].searchable_text().lower()
    assert "python" in hi.keywords

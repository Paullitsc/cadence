"""Job↔profile matching: fit scoring, top-K bullet retrieval, JD keyword extraction.

All pure functions (no network, no LLM). Keyword extraction is deterministic
noun-phrase-ish frequency scoring biased toward a known tech vocabulary plus the
candidate's own skills/tags — cheap and reproducible, as the assignment asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Job
from .embeddings import Embedder, cosine
from .models import BulletRef, MasterResume

# Keep intra-word punctuation so "c++", "c#", "ci-cd", "node.js" survive, but do NOT
# swallow trailing sentence punctuation ("kafka." -> "kafka", "internship." -> ...).
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.+#-][a-z0-9]+)*[+#]*")

STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or of to in on for with at by from as is are be been being this that
    these those it its we you your our their they them he she his her i me my will would
    can could should may might must have has had do does did not no nvm than then so
    such into over under about above below up down out off again further once here there
    all any both each few more most other some own same too very just also who whom which
    what when where why how if but because until while during before after between within
    across per via etc eg ie us role team work working help support strong ability
    experience experiences requirement requirements responsibility responsibilities
    plus year years month months day days including include includes required preferred
    ideal candidate candidates looking join company opportunity opportunities position
    intern internship internships know nice
    """.split()
)

# Small seed vocabulary of common tech keywords. The candidate's real skills/tags are
# added on top, so extraction favors terms that are genuinely relevant to the profile.
_TECH_VOCAB: frozenset[str] = frozenset(
    """
    python java javascript typescript c c++ c# go golang rust ruby php scala kotlin swift
    sql nosql postgres postgresql mysql sqlite mongodb redis kafka spark hadoop airflow
    react angular vue node node.js express django flask fastapi spring rails dotnet .net
    aws gcp azure docker kubernetes k8s terraform linux git ci-cd graphql rest grpc api
    backend frontend fullstack full-stack distributed-systems microservices ml ai llm
    machine-learning data data-engineering etl pipelines pipeline testing automation
    tensorflow pytorch pandas numpy html css agile
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, preserving tech punctuation (c++, c#, node.js)."""
    return _TOKEN_RE.findall(text.lower())


def content_tokens(text: str) -> set[str]:
    """Meaningful tokens (stopwords removed) — the grounding/keyword unit."""
    return {t for t in tokenize(text) if t not in STOPWORDS and len(t) > 1}


def job_text(job: Job) -> str:
    """Embeddable text for a job: the real description if present, else title/company."""
    parts = [job.title, job.company_name, *job.locations]
    if job.description:
        parts.append(job.description)
    return " ".join(p for p in parts if p)


def extract_keywords(jd_text: str, resume: MasterResume | None = None, top_n: int = 15) -> list[str]:
    """Extract hard requirement/keyword candidates from a job description.

    Deterministic: count unigrams + bigrams, drop stopwords, then rank — terms that
    match the known tech vocabulary (seed list + the candidate's real skills/tags)
    are boosted so the extracted keywords are the genuinely relevant ones.
    """
    vocab = set(_TECH_VOCAB)
    if resume is not None:
        for skill in resume.skills.all():
            vocab.update(content_tokens(skill))
        for exp in resume.experiences:
            for b in exp.bullets:
                vocab.update(t.lower() for t in b.tags)
        for proj in resume.projects:
            for b in proj.bullets:
                vocab.update(t.lower() for t in b.tags)

    toks = [t for t in tokenize(jd_text) if t not in STOPWORDS and len(t) > 1]
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for i, tok in enumerate(toks):
        order.setdefault(tok, i)
        scores[tok] = scores.get(tok, 0.0) + (3.0 if tok in vocab else 1.0)
    # Bigrams like "distributed systems", "machine learning".
    for i in range(len(toks) - 1):
        bigram = f"{toks[i]} {toks[i + 1]}"
        order.setdefault(bigram, i)
        scores[bigram] = scores.get(bigram, 0.0) + 1.5

    ranked = sorted(scores, key=lambda k: (-scores[k], order[k]))
    return ranked[:top_n]


@dataclass
class MatchResult:
    """Outcome of scoring one job against the profile."""

    fit_score: float  # job-to-profile fit in [0, 1] (mean of the top-K bullet sims)
    top_bullets: list[BulletRef]  # most relevant bullets, best first
    keywords: list[str]
    # The JD embedding computed for scoring, kept so Phase-5 CV grouping can cluster
    # similar jobs without re-embedding anything (zero extra cost).
    jd_vector: list[float] = field(default_factory=list)


def score_job(
    job: Job,
    bullets: list[BulletRef],
    bullet_vectors: list[list[float]],
    embedder: Embedder,
    *,
    resume: MasterResume | None = None,
    top_k: int = 8,
) -> MatchResult:
    """Score a job against the profile and retrieve its most relevant bullets.

    ``bullet_vectors`` are precomputed once per run (aligned with ``bullets``) so we
    only embed the JD here. ``fit_score`` is the mean cosine similarity of the top-K
    bullets, clamped to [0, 1]; ``top_bullets`` are those bullets, best first.
    """
    if not bullets:
        return MatchResult(fit_score=0.0, top_bullets=[], keywords=[], jd_vector=[])

    jd_vec = embedder.embed_one(job_text(job))
    sims = [(cosine(jd_vec, bv), ref) for bv, ref in zip(bullet_vectors, bullets)]
    sims.sort(key=lambda pair: pair[0], reverse=True)
    top = sims[: max(1, top_k)]

    top_bullets = [ref for _, ref in top]
    mean_sim = sum(s for s, _ in top) / len(top)
    fit = max(0.0, min(1.0, mean_sim))
    keywords = extract_keywords(job_text(job), resume=resume)
    return MatchResult(
        fit_score=round(fit, 4), top_bullets=top_bullets, keywords=keywords, jd_vector=jd_vec
    )

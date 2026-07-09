"""CV grouping + cache keys — the Phase-5 LLM cost saver. All pure functions.

Similar jobs don't need separately-tailored CVs. Two mechanisms, both deterministic:

* **Within-run clustering**: greedily cluster the capped job list on cosine
  similarity of the JD embeddings ``match_and_slice`` ALREADY computed (zero extra
  cost), with keyword-set overlap as a sanity check so two superficially-similar
  JDs with different hard requirements don't collapse. One cluster = one tailoring
  call + one render + one Drive upload, made for the highest-fit member (the
  representative); every other member reuses that CV.
* **Cross-run cache key**: a stable hash of (selected bullet ids + normalized
  keyword set) — everything that determines the tailoring INPUT. If a future run
  produces the same key, the stored CV (``cv_cache`` table) is reused outright.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .embeddings import cosine


def keyword_overlap(a: list[str], b: list[str]) -> float:
    """Jaccard overlap of two keyword lists (case-insensitive). 0.0 if either is empty."""
    sa = {k.strip().lower() for k in a if k.strip()}
    sb = {k.strip().lower() for k in b if k.strip()}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class CvCluster:
    """One group of jobs sharing a tailored CV. Indices point into the input list."""

    representative: int  # index of the highest-fit member (tailor/render/upload once)
    members: list[int]  # every index in the cluster, representative first


def cluster_jobs(
    jd_vectors: list[list[float]],
    keyword_sets: list[list[str]],
    *,
    similarity_threshold: float = 0.9,
    keyword_overlap_threshold: float = 0.5,
) -> list[CvCluster]:
    """Greedily cluster jobs by JD-embedding similarity + keyword overlap.

    Inputs are parallel lists ordered BEST FIT FIRST (as ``match_and_slice`` sorts
    them), so the first job assigned to each cluster is its highest-fit member and
    becomes the representative. A job joins a cluster only if it clears BOTH the
    cosine-similarity threshold against the representative's JD vector AND the
    keyword-overlap sanity check. Deterministic: same inputs, same clusters.
    """
    clusters: list[CvCluster] = []
    for i in range(len(jd_vectors)):
        placed = False
        for cluster in clusters:
            rep = cluster.representative
            if not jd_vectors[i] or not jd_vectors[rep]:
                continue  # no vector (e.g. empty profile) → never group
            if cosine(jd_vectors[i], jd_vectors[rep]) < similarity_threshold:
                continue
            if keyword_overlap(keyword_sets[i], keyword_sets[rep]) < keyword_overlap_threshold:
                continue
            cluster.members.append(i)
            placed = True
            break
        if not placed:
            clusters.append(CvCluster(representative=i, members=[i]))
    return clusters


# Bump when the rendered CV's LAYOUT changes (e.g. the switch from the RenderCV
# compact design to the Resume.tex LaTeX template): a cached CV embeds the layout
# it was rendered with, so a layout change must invalidate old entries or cache
# hits keep resurfacing the old-look PDFs.
CV_LAYOUT_VERSION = "latex-v1"


def cv_cache_key(bullet_ids: list[str], keywords: list[str]) -> str:
    """Stable cache key for one tailored CV: the tailoring input's identity.

    Selected bullet ids and keywords are treated as SETS (sorted, normalized) so
    retrieval-order jitter doesn't defeat the cache. Same bullets + same keywords
    ⇒ same key ⇒ the stored CV is reused with no LLM call. The layout version is
    salted in so a design change re-tailors (once per cluster) instead of reusing
    stale-layout artifacts.
    """
    ids = ",".join(sorted({b.strip() for b in bullet_ids if b.strip()}))
    kws = ",".join(sorted({k.strip().lower() for k in keywords if k.strip()}))
    digest = hashlib.sha256(
        f"layout:{CV_LAYOUT_VERSION}|bullets:{ids}|keywords:{kws}".encode("utf-8")
    )
    return digest.hexdigest()[:24]

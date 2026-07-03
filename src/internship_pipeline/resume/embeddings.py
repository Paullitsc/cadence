"""Pluggable text embeddings for job↔bullet matching.

Default backend is local ``sentence-transformers`` (cheap/free, no API cost). If
that library is not installed, we fall back to a deterministic, dependency-free
hashing embedder so the pipeline still runs offline with zero setup (lower quality,
but correct and reproducible). The interface is a small ABC so a hosted embedding
model could be dropped in later.

    # VERIFY (hosted alternative): a hosted embedding model (e.g. Voyage AI
    # `voyage-3`, ~USD $0.06 / 1M tokens) would improve match quality at a small
    # per-run cost. Cost + exact model id would need confirming against the live
    # pricing page before wiring it in — not used by default.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

from ..config import Settings
from ..logging_config import get_logger

log = get_logger(__name__)


class Embedder(ABC):
    """Turns texts into fixed-length vectors."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 if either is empty)."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class HashingEmbedder(Embedder):
    """Deterministic bag-of-words embedder — no dependencies, no network.

    Each token is hashed into one of ``dim`` buckets and accumulated (like the
    hashing trick). Vectors are non-negative, so cosine similarity lands in [0, 1].
    Used in tests and whenever sentence-transformers is unavailable.
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        # Local import avoids a circular import (matching imports embeddings).
        from .matching import tokenize

        vec = [0.0] * self.dim
        for token in tokenize(text):
            h = hashlib.sha1(token.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0  # signed hashing reduces collisions
            vec[idx] += sign
        return vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class SentenceTransformerEmbedder(Embedder):
    """Local sentence-transformers backend (lazy-loaded)."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # heavy; lazy import

        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


def get_embedder(settings: Settings) -> Embedder:
    """Construct the configured embedder, degrading gracefully to hashing.

    Mirrors the storage factory: if sentence-transformers is requested but not
    importable, log and fall back to the deterministic hashing embedder.
    """
    if settings.embedding_backend == "sentence_transformers":
        try:
            return SentenceTransformerEmbedder(settings.embedding_model)
        except Exception as exc:  # ImportError, model download failure, etc.
            log.warning(
                "sentence-transformers unavailable; falling back to hashing embedder",
                extra={"error": repr(exc)},
            )
    return HashingEmbedder()

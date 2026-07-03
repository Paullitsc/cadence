"""Phase 2: résumé slicing + application drafting.

Pure/parse vs. side-effecting split mirrors ``sourcing/``: matching, keyword
extraction, tailoring grounding, and RenderCV assembly are testable pure functions;
the LLM call and PDF render are the only side effects, both dependency-injected or
lazy-imported so the pipeline (and the test suite) runs offline with zero creds.
"""

from __future__ import annotations

from .answers import DEFAULT_QUESTIONS, draft_common_answers
from .embeddings import Embedder, cosine, get_embedder
from .loader import all_bullets, load_master_resume
from .matching import MatchResult, extract_keywords, score_job
from .models import BulletRef, MasterResume
from .rendercv import build_rendercv_cv, to_yaml, write_and_render
from .tailoring import TailorResult, tailor_resume

__all__ = [
    "DEFAULT_QUESTIONS",
    "draft_common_answers",
    "Embedder",
    "cosine",
    "get_embedder",
    "all_bullets",
    "load_master_resume",
    "MatchResult",
    "extract_keywords",
    "score_job",
    "BulletRef",
    "MasterResume",
    "build_rendercv_cv",
    "to_yaml",
    "write_and_render",
    "TailorResult",
    "tailor_resume",
]

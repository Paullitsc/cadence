"""Phase 2: résumé slicing + application drafting.

Pure/parse vs. side-effecting split mirrors ``sourcing/``: matching, keyword
extraction, tailoring grounding, and CV/LaTeX assembly are testable pure functions;
the LLM call and PDF compile are the only side effects, both dependency-injected or
lazy-imported so the pipeline (and the test suite) runs offline with zero creds.
"""

from __future__ import annotations

from .embeddings import Embedder, cosine, get_embedder
from .loader import all_bullets, load_master_resume
from .matching import MatchResult, extract_keywords, score_job
from .models import BulletRef, MasterResume
from .render import build_cv_doc, to_yaml, write_and_render, write_and_render_one_page
from .tailoring import TailorResult, tailor_resume

__all__ = [
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
    "build_cv_doc",
    "to_yaml",
    "write_and_render",
    "write_and_render_one_page",
    "TailorResult",
    "tailor_resume",
]

from __future__ import annotations

import pytest

from internship_pipeline.config import Settings


@pytest.fixture
def settings() -> Settings:
    # Ignore any real .env so tests are deterministic.
    return Settings(_env_file=None)


@pytest.fixture(autouse=True)
def _no_latex_engine(monkeypatch):
    """Hide any real LaTeX engine from the suite.

    A developer machine with tectonic installed would otherwise really compile
    PDFs inside stage tests (slow, and a cold tectonic cache downloads packages
    from the network — tests must stay offline). Tests that exercise the compile
    path re-patch ``latex.shutil.which`` themselves with a fake engine.
    """
    import internship_pipeline.resume.latex as latex

    monkeypatch.setattr(latex.shutil, "which", lambda _name: None)

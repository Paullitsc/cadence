from __future__ import annotations

import pytest

from internship_pipeline.config import Settings


@pytest.fixture
def settings() -> Settings:
    # Ignore any real .env so tests are deterministic.
    return Settings(_env_file=None)

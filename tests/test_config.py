from __future__ import annotations

import pytest
from pydantic import ValidationError

from internship_pipeline.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.log_level == "INFO"
    assert s.storage_backend == "sqlite"
    assert s.anthropic_api_key is None


def test_env_override(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    s = Settings(_env_file=None)
    assert s.log_level == "DEBUG"
    assert s.storage_backend == "supabase"


def test_invalid_storage_backend_rejected(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "mysql")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

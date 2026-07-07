from __future__ import annotations

import pytest
from pydantic import ValidationError

from internship_pipeline.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.log_level == "INFO"
    assert s.storage_backend == "sqlite"
    assert s.anthropic_api_key is None


def test_source_url_lists_parse_comma_separated(monkeypatch):
    monkeypatch.setenv("EXTRA_LISTINGS_URLS", " https://a/l.json , https://b/l.json ,")
    monkeypatch.setenv("GITHUB_README_URLS", "")
    s = Settings(_env_file=None)
    assert s.extra_listings_url_list == ["https://a/l.json", "https://b/l.json"]
    assert s.github_readme_url_list == []  # blank -> no feeds, not [""]


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

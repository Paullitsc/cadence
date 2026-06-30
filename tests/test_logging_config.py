from __future__ import annotations

import json
import logging

from internship_pipeline.logging_config import JsonFormatter, configure_logging


def _record(**extra) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_formats_valid_json_with_core_fields():
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["msg"] == "hello world"
    assert "ts" in payload


def test_includes_extra_fields():
    payload = json.loads(JsonFormatter().format(_record(run_id="abc123", stage="source")))
    assert payload["run_id"] == "abc123"
    assert payload["stage"] == "source"


def test_configure_logging_sets_single_stdout_json_handler():
    root = configure_logging("DEBUG")
    try:
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers.clear()

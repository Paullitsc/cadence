"""parse_json_object: fences/prose tolerated, truncated responses salvaged.

Regression for the July 2026 outage: tailoring responses truncated at
``max_tokens`` raised ``ValueError("no JSON object found...")`` and killed the
whole ``match_and_slice`` stage — days of runs prepared zero applications.
"""

from __future__ import annotations

import pytest

from internship_pipeline.resume.llm import parse_json_object


def test_parses_plain_object():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parses_fenced_object_with_prose():
    text = 'Sure! Here you go:\n```json\n{"selected": []}\n```\nHope that helps.'
    assert parse_json_object(text) == {"selected": []}


def test_parses_object_embedded_in_prose():
    assert parse_json_object('The answer is {"a": [1, 2]} as requested.') == {"a": [1, 2]}


def test_salvages_response_truncated_mid_element():
    # Cut off mid-string inside the third element — the two complete bullets survive.
    text = (
        '{"selected": ['
        '{"id": "e1-b1", "text": "Built pipelines"}, '
        '{"id": "e2-b1", "text": "Shipped services"}, '
        '{"id": "e3-b1", "te'
    )
    obj = parse_json_object(text)
    assert obj == {
        "selected": [
            {"id": "e1-b1", "text": "Built pipelines"},
            {"id": "e2-b1", "text": "Shipped services"},
        ]
    }


def test_salvages_truncation_with_escapes_and_nested_brackets():
    text = '{"a": {"quote": "say \\"hi\\" {not json}"}, "b": [1, {"c": 2}], "d": [3,'
    obj = parse_json_object(text)
    assert obj["a"] == {"quote": 'say "hi" {not json}'}
    assert obj["b"] == [1, {"c": 2}]


def test_unsalvageable_text_still_raises():
    with pytest.raises(ValueError):
        parse_json_object("I cannot produce JSON for this request.")
    with pytest.raises(ValueError):
        parse_json_object('{"broken": }')

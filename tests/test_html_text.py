from __future__ import annotations

from internship_pipeline.sourcing.html_text import html_to_text


def test_strips_literal_tags():
    assert html_to_text("<p>Build things.</p>") == "Build things."


def test_unescapes_entities_before_stripping():
    # Greenhouse's `content` field is HTML-entity-escaped HTML — the tags
    # aren't real tags until entities are decoded.
    assert html_to_text("&lt;p&gt;Build things.&lt;/p&gt;") == "Build things."


def test_collapses_whitespace_across_tags():
    assert html_to_text("<h2>Title</h2>\n<p>Body   text.</p>") == "Title Body text."


def test_empty_input_returns_empty_string():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""

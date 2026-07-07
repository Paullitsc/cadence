"""Minimal HTML -> plain text for job-description fields that only expose HTML.

Stdlib only (no bs4/lxml dependency) — good enough for the matching/keyword-
extraction input, which only tokenizes this text and never renders it.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Optional


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


def html_to_text(raw: Optional[str]) -> str:
    """Strip tags and collapse whitespace. Unescapes entities first — Greenhouse's
    ``content`` field is HTML-entity-ESCAPED HTML (``&lt;p&gt;...&lt;/p&gt;``), so
    the tags aren't real tags until entities are decoded.
    """
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(raw))
    parser.close()
    return re.sub(r"\s+", " ", parser.text()).strip()

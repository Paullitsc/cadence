"""Small helpers shared across sourcing modules (no single feed owns these)."""

from __future__ import annotations

from urllib.parse import urlsplit


def repo_slug(url: str) -> str:
    """``owner/repo`` from a raw.githubusercontent.com URL (fallback: host+path)."""
    parts = urlsplit(url)
    segments = [p for p in parts.path.split("/") if p]
    if parts.netloc == "raw.githubusercontent.com" and len(segments) >= 2:
        return f"{segments[0]}/{segments[1]}"
    return f"{parts.netloc}{parts.path}"

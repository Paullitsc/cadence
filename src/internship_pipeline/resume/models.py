"""Typed models for the tagged master résumé (Phase 2 owns this shape).

Mirrors the blueprint's Step-1 schema: every bullet is an object with ``text``,
``tags``, and ``metrics``. This YAML is the single source of truth — tailoring only
ever selects/reorders/rephrases these real bullets.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Bullet(BaseModel):
    """One résumé bullet — a real, factual accomplishment."""

    text: str
    tags: list[str] = Field(default_factory=list)
    metrics: bool = False


class Education(BaseModel):
    institution: str
    area: Optional[str] = None
    degree: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    highlights: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    company: str
    role: str
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    name: str
    url: Optional[str] = None
    bullets: list[Bullet] = Field(default_factory=list)


class Skills(BaseModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    def all(self) -> list[str]:
        return [*self.languages, *self.frameworks, *self.tools]


class Links(BaseModel):
    linkedin: Optional[str] = None
    github: Optional[str] = None
    website: Optional[str] = None


class MasterResume(BaseModel):
    """The full tagged master résumé."""

    placeholder: bool = False
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: Links = Field(default_factory=Links)
    summary: Optional[str] = None
    education: list[Education] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)


class BulletRef(BaseModel):
    """A bullet plus a stable id and a pointer to its parent entry.

    ``id`` is what the tailoring LLM references (so it can select/reorder without
    ever emitting free-form bullet text of its own). ``source`` is "experience" or
    "project"; ``parent`` is the company/project name it belongs to.
    """

    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    metrics: bool = False
    source: str  # "experience" | "project"
    parent: str  # company or project name (for regrouping in the rendered CV)

    def searchable_text(self) -> str:
        """Text used for embedding + grounding (bullet text plus its tags).

        Markdown ``**bold**`` markers are stripped so hand-bolded keywords in
        ``master_resume.yaml`` don't perturb embeddings or token matching.
        """
        return f"{self.text.replace('**', '')} {' '.join(self.tags)}".strip()

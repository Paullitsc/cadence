from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.resume.models import Bullet, Project

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")


def test_loads_and_validates_master_resume():
    resume = load_master_resume(FIXTURE)
    assert resume.name == "Test Candidate"
    assert resume.placeholder is False
    assert resume.skills.languages == ["Python", "Java", "SQL"]
    assert len(resume.experiences) == 1
    assert len(resume.projects) == 1


def test_all_bullets_assigns_stable_ids_across_experiences_and_projects():
    resume = load_master_resume(FIXTURE)
    refs = all_bullets(resume)
    # two experience bullets + one project bullet
    assert len(refs) == 3
    assert refs[0].source == "experience" and refs[0].parent == "Acme Labs"
    assert refs[-1].source == "project" and refs[-1].parent == "Query Planner"
    # searchable_text folds in tags for embedding/grounding
    assert "kafka" in refs[0].searchable_text().lower()
    # ids are unique and deterministic across repeated loads
    ids = [r.id for r in refs]
    assert len(set(ids)) == len(ids)
    assert ids == [r.id for r in all_bullets(load_master_resume(FIXTURE))]


def test_bullet_ids_survive_reordering_master_resume():
    """Regression: ids used to encode list position (``p{index}b{index}``), so
    inserting a new project before an existing one silently reattributed the
    existing project's already-tailored bullets to the wrong project on the next
    render. Ids must depend only on (source, parent, text), not position.
    """
    resume = load_master_resume(FIXTURE)
    before = {r.parent: r.id for r in all_bullets(resume) if r.source == "project"}

    new_project = Project(name="Brand New Project", bullets=[Bullet(text="Did a new thing.")])
    reordered = resume.model_copy(update={"projects": [new_project, *resume.projects]})
    after = {r.parent: r.id for r in all_bullets(reordered) if r.source == "project"}

    assert before["Query Planner"] == after["Query Planner"]


def test_missing_file_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_master_resume("does/not/exist.yaml")

from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.resume.loader import all_bullets, load_master_resume

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
    ids = [r.id for r in refs]
    # two experience bullets + one project bullet
    assert ids == ["e0b0", "e0b1", "p0b0"]
    assert refs[0].source == "experience" and refs[0].parent == "Acme Labs"
    assert refs[-1].source == "project" and refs[-1].parent == "Query Planner"
    # searchable_text folds in tags for embedding/grounding
    assert "kafka" in refs[0].searchable_text().lower()


def test_missing_file_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_master_resume("does/not/exist.yaml")

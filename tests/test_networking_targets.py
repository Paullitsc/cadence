"""Targets loading + person seeding: positional ids, placeholder rows, and the
committed 8VC seed file staying loadable."""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.networking.models import (
    allowed_human_transition,
    make_person_id,
)
from internship_pipeline.networking.targets import load_targets, seed_people

REPO_TARGETS = Path(__file__).parent.parent / "networking_targets.yaml"


def test_make_person_id_slugs():
    assert make_person_id("8vc", "AI21 Labs", 1) == "8vc-ai21-labs-1"
    assert make_person_id("8vc", "Coram.ai", 2) == "8vc-coram-ai-2"


def test_human_transitions_forward_only():
    assert allowed_human_transition("connect_drafted", "connect_sent") is True
    assert allowed_human_transition("connect_sent", "accepted") is True
    assert allowed_human_transition("queued", "closed") is True
    assert allowed_human_transition("connect_sent", "replied") is True  # jumps forward ok
    # Backward or pipeline-owned targets are rejected.
    assert allowed_human_transition("accepted", "connect_sent") is False
    assert allowed_human_transition("queued", "connect_drafted") is False  # pipeline's
    assert allowed_human_transition("connect_sent", "connect_sent") is False
    # The one deliberate exception: a late accept revives a stalled thread.
    assert allowed_human_transition("email_due", "accepted") is True
    assert allowed_human_transition("email_due", "connect_sent") is False


def test_missing_file_is_a_clean_noop(tmp_path):
    campaign, targets = load_targets(tmp_path / "nope.yaml")
    assert campaign == "" and targets == []


def test_malformed_rows_are_skipped(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(
        """
campaign: test
companies:
  - name: Good Co
    tier: 1
  - tier: "not a company"
  - name: Also Good
""",
        encoding="utf-8",
    )
    campaign, targets = load_targets(p)
    assert campaign == "test"
    assert [t.name for t in targets] == ["Good Co", "Also Good"]


def test_seed_people_placeholder_and_positional_ids(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(
        """
campaign: test
companies:
  - name: Empty Co
    tier: 1
    domain: empty.co
  - name: Full Co
    blurb: Builds robots.
    people:
      - name: Jane Doe
        role: CTO
        linkedin: https://linkedin.com/in/janedoe
      - name: Sam Lee
""",
        encoding="utf-8",
    )
    campaign, targets = load_targets(p)
    people = seed_people(campaign, targets)
    by_id = {p.person_id: p for p in people}
    assert set(by_id) == {"test-empty-co-1", "test-full-co-1", "test-full-co-2"}

    placeholder = by_id["test-empty-co-1"]
    assert placeholder.has_identity() is False
    assert placeholder.status == "queued"
    assert placeholder.company_domain == "empty.co"

    jane = by_id["test-full-co-1"]
    assert jane.name == "Jane Doe" and jane.role == "CTO"
    assert jane.has_identity() is True
    assert jane.company_blurb == "Builds robots."


def test_committed_8vc_seed_loads():
    campaign, targets = load_targets(REPO_TARGETS)
    assert campaign == "8vc"
    assert len(targets) > 100  # the full portfolio, minus exited companies
    names = {t.name for t in targets}
    assert "Anduril" in names
    assert "Palantir" not in names  # marked Exited on the live page
    assert all(t.tier in (1, 2, 3) for t in targets)
    # Every row seeds exactly one placeholder person (no people listed yet).
    people = seed_people(campaign, targets)
    assert len(people) == len(targets)
    assert len({p.person_id for p in people}) == len(people)

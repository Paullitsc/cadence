"""The networking stage end-to-end offline: seeding, budgeted drafting, the
accepted→message flow, escalation timers, idempotency, and the digest section —
all with zero credentials (SQLite, deterministic copy, no sheet)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.digest import render_digest, render_digest_text
from internship_pipeline.models import StageContext
from internship_pipeline.networking.models import (
    STATUS_ACCEPTED,
    STATUS_CONNECT_DRAFTED,
    STATUS_CONNECT_SENT,
    STATUS_EMAIL_DUE,
    STATUS_MESSAGE_DRAFTED,
)
from internship_pipeline.networking.sequence import outstanding_actions
from internship_pipeline.stages import networking

RESUME_FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")

TARGETS_YAML = """
campaign: test
companies:
  - name: Alpha Robotics
    tier: 1
    blurb: Alpha Robotics builds data pipelines for robots in Python.
    people:
      - name: Jane Doe
        role: CTO
        linkedin: https://linkedin.com/in/janedoe
  - name: Beta Systems
    tier: 2
    blurb: Beta Systems builds developer tools.
    people:
      - name: Sam Lee
  - name: Gamma Unknown
    tier: 1
"""


@pytest.fixture
def stage_settings(tmp_path) -> Settings:
    targets = tmp_path / "targets.yaml"
    targets.write_text(TARGETS_YAML, encoding="utf-8")
    return Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        networking_targets_file=str(targets),
        master_resume_file=RESUME_FIXTURE,
        networking_daily_connects=5,
        anthropic_api_key=None,  # deterministic drafting
        tracker_sheets_enabled=False,  # no sheet — storage only
    )


def _run(settings: Settings) -> tuple[StageContext, object]:
    ctx = StageContext(run_id="test-run", settings=settings)
    result = networking.run(ctx)
    return ctx, result


def test_missing_targets_file_noops(tmp_path):
    s = Settings(
        _env_file=None,
        database_path=str(tmp_path / "pipeline.db"),
        networking_targets_file=str(tmp_path / "missing.yaml"),
    )
    _, result = _run(s)
    assert result.notes == "no networking targets"
    assert result.counts["networking_connects_drafted"] == 0


def test_seeds_and_drafts_within_budget(stage_settings):
    ctx, result = _run(stage_settings)
    assert result.counts["networking_people_seeded"] == 3
    # Two people have an identity → both get connect notes (budget 5); the
    # placeholder company waits for a human pick.
    assert result.counts["networking_connects_drafted"] == 2

    storage = ctx.get_storage()
    people = {p.person_id: p for p in storage.list_people()}
    jane = people["test-alpha-robotics-1"]
    assert jane.status == STATUS_CONNECT_DRAFTED
    assert jane.draft_kind == "connect"
    assert "Alpha Robotics" in jane.draft_body
    assert jane.status_changed_at
    assert people["test-gamma-unknown-1"].status == "queued"

    # Idempotent: a second run seeds nothing and drafts nothing new.
    _, again = _run(stage_settings)
    assert again.counts["networking_people_seeded"] == 0
    assert again.counts["networking_connects_drafted"] == 0


def test_budget_caps_outstanding_drafts(stage_settings):
    stage_settings = stage_settings.model_copy(update={"networking_daily_connects": 1})
    ctx, result = _run(stage_settings)
    assert result.counts["networking_connects_drafted"] == 1
    people = {p.person_id: p for p in ctx.get_storage().list_people()}
    # Tier 1 wins the single slot.
    assert people["test-alpha-robotics-1"].status == STATUS_CONNECT_DRAFTED
    assert people["test-beta-systems-1"].status == "queued"


def test_accepted_person_gets_message_drafted(stage_settings):
    ctx, _ = _run(stage_settings)
    storage = ctx.get_storage()
    jane = storage.get_person("test-alpha-robotics-1")
    jane.status = STATUS_ACCEPTED  # as the sheet merge would after the human marks it
    storage.save_person(jane)

    _, result = _run(stage_settings)
    assert result.counts["networking_messages_drafted"] == 1
    jane = storage.get_person("test-alpha-robotics-1")
    assert jane.status == STATUS_MESSAGE_DRAFTED
    assert jane.draft_kind == "message"
    assert "Alpha Robotics" in jane.draft_body
    assert "data pipeline" in jane.draft_body  # the real, relevant bullet


def test_stale_connect_escalates_to_email_due(stage_settings):
    ctx, _ = _run(stage_settings)
    storage = ctx.get_storage()
    jane = storage.get_person("test-alpha-robotics-1")
    jane.status = STATUS_CONNECT_SENT
    jane.status_changed_at = (
        datetime.now(timezone.utc) - timedelta(days=11)
    ).isoformat()
    storage.save_person(jane)

    _, result = _run(stage_settings)
    assert result.counts["networking_escalated"] == 1
    jane = storage.get_person("test-alpha-robotics-1")
    assert jane.status == STATUS_EMAIL_DUE
    assert jane.draft_body == "" and jane.draft_kind is None


def test_sqlite_list_people_filters_and_orders(stage_settings):
    ctx, _ = _run(stage_settings)
    storage = ctx.get_storage()
    drafted = storage.list_people(status=STATUS_CONNECT_DRAFTED)
    assert {p.person_id for p in drafted} == {
        "test-alpha-robotics-1",
        "test-beta-systems-1",
    }
    everyone = storage.list_people()
    assert [p.tier for p in everyone] == sorted(p.tier for p in everyone)


def test_digest_renders_networking_actions(stage_settings):
    ctx, _ = _run(stage_settings)
    people = ctx.get_storage().list_people()
    actions = outstanding_actions(people)
    assert actions  # the two drafted connects

    html = render_digest(
        jobs=[], run_id="test-run",
        counts={"networking_awaiting_person": 1}, networking_actions=actions,
    )
    assert "Networking — today's LinkedIn actions" in html
    assert "Alpha Robotics" in html and "Jane Doe" in html
    assert "would love to connect" in html  # the draft body is right in the email
    assert "need a person picked" in html

    text = render_digest_text(jobs=[], run_id="test-run", networking_actions=actions)
    assert "Networking actions ready (LinkedIn, sent by you): 2" in text


def test_digest_without_networking_stays_clean():
    html = render_digest(jobs=[], run_id="test-run")
    assert "Networking — today's LinkedIn actions" not in html

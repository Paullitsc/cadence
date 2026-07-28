"""Two-way roster sync: networking_targets.yaml <-> storage/sheet.

Covers ``networking/merge.py`` (who wins, per field) and the file round-trip in
``networking/targets.py`` that makes the writeback safe to commit. No network,
no Google — the sheet side arrives as the ``human_edited`` mapping that
``rows.apply_sheet_edits`` produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from internship_pipeline.networking.merge import merge_identity
from internship_pipeline.networking.models import Person
from internship_pipeline.networking.targets import (
    dump_targets,
    load_targets,
    seed_people,
    write_targets,
)

# The real roster is git-ignored (public repo, real people's names — it is
# materialized in CI from a secret), so the committed fixture is what CI checks.
SAMPLE_TARGETS = Path(__file__).parent / "fixtures" / "networking_targets_sample.yaml"
REPO_TARGETS = Path(__file__).parent.parent / "networking_targets.yaml"

TARGETS_YAML = """\
campaign: test
companies:
- name: Empty Co
  tier: 1
  domain: empty.co
- name: Full Co
  tier: 2
  blurb: Builds robots.
  people:
  - name: Jane Doe
    role: CTO
    linkedin: https://linkedin.com/in/janedoe
  - name: Sam Lee
"""


def _write(tmp_path, text=TARGETS_YAML):
    p = tmp_path / "targets.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _load(tmp_path, text=TARGETS_YAML):
    """``(path, campaign, targets, {person_id: Person})`` seeded from the file."""
    p = _write(tmp_path, text)
    campaign, targets = load_targets(p)
    return p, campaign, targets, {x.person_id: x for x in seed_people(campaign, targets)}


# --- file -> storage (the direction that used to be silently ignored) ---------


def test_yaml_edit_reaches_storage_even_when_the_field_is_already_set(tmp_path):
    _, campaign, targets, people = _load(tmp_path)
    stored = people["test-full-co-1"]
    stored.name = "Jane Doe"
    targets[1].people[0].name = "Jane D. Doe"  # the human corrected the file
    targets[1].people[0].role = "Co-founder & CTO"

    result = merge_identity(campaign, targets, list(people.values()), human_edited={})

    assert stored.name == "Jane D. Doe"
    assert stored.role == "Co-founder & CTO"
    assert result.pulled == 2
    assert result.yaml_changed is False  # nothing to push back
    assert stored in result.people_changed


def test_blank_file_field_never_clears_a_stored_value(tmp_path):
    _, campaign, targets, people = _load(tmp_path)
    stored = people["test-full-co-2"]  # Sam Lee, no role/linkedin in the file
    stored.role = "Head of Data"

    result = merge_identity(campaign, targets, list(people.values()), human_edited={})

    assert stored.role == "Head of Data"  # not erased
    assert targets[1].people[1].role == "Head of Data"  # pushed into the file instead
    assert result.pushed == 1


# --- sheet -> file -----------------------------------------------------------


def test_person_named_on_the_sheet_materializes_a_people_entry(tmp_path):
    path, campaign, targets, people = _load(tmp_path)
    placeholder = people["test-empty-co-1"]  # a company with nobody listed
    placeholder.name = "Ada Lovelace"
    placeholder.role = "VP Eng"
    placeholder.linkedin_url = "https://linkedin.com/in/ada"

    result = merge_identity(
        campaign, targets, list(people.values()),
        human_edited={"test-empty-co-1": {"name", "role", "linkedin_url"}},
    )
    assert result.yaml_changed is True
    write_targets(path, campaign, targets)

    _, reloaded = load_targets(path)
    ada = reloaded[0].people[0]
    assert (ada.name, ada.role, ada.linkedin) == (
        "Ada Lovelace", "VP Eng", "https://linkedin.com/in/ada"
    )
    # The materialized entry claims the placeholder's id — the row keeps its identity.
    assert {p.person_id for p in seed_people(campaign, reloaded)} >= {"test-empty-co-1"}


def test_sheet_edit_wins_over_the_file_for_that_run_only(tmp_path):
    _, campaign, targets, people = _load(tmp_path)
    stored = people["test-full-co-1"]
    stored.name = "Jane Smith"  # the human retyped the cell this run

    result = merge_identity(
        campaign, targets, list(people.values()), human_edited={"test-full-co-1": {"name"}}
    )

    assert targets[1].people[0].name == "Jane Smith"  # file follows the sheet
    assert stored.name == "Jane Smith"
    assert result.pushed == 1 and result.pulled == 0


def test_roster_converges_after_one_round_trip(tmp_path):
    """The same edit must not ping-pong: a second pass is a no-op."""
    path, campaign, targets, people = _load(tmp_path)
    people["test-empty-co-1"].name = "Ada Lovelace"
    merge_identity(
        campaign, targets, list(people.values()), human_edited={"test-empty-co-1": {"name"}}
    )
    write_targets(path, campaign, targets)

    campaign2, targets2 = load_targets(path)
    again = merge_identity(campaign2, targets2, list(people.values()), human_edited={})
    assert (again.pulled, again.pushed, again.yaml_changed) == (0, 0, False)
    assert write_targets(path, campaign2, targets2) is False  # file untouched


# --- safety rails ------------------------------------------------------------


def test_person_whose_company_left_the_file_is_left_alone(tmp_path):
    _, campaign, targets, _ = _load(tmp_path)
    orphan = Person(person_id="test-gone-co-1", campaign=campaign,
                    company_name="Gone Co", name="Old Contact")

    result = merge_identity(campaign, targets, [orphan], human_edited={})

    assert orphan.name == "Old Contact"  # stored history survives a file deletion
    assert (result.pulled, result.pushed, result.yaml_changed) == (0, 0, False)


def test_placeholder_without_identity_adds_nothing_to_the_file(tmp_path):
    _, campaign, targets, people = _load(tmp_path)
    result = merge_identity(campaign, targets, list(people.values()), human_edited={})
    assert result.yaml_changed is False
    assert targets[0].people == []


# --- file round-trip ---------------------------------------------------------


def test_write_preserves_the_schema_doc_header(tmp_path):
    path = _write(tmp_path, "# Schema docs.\n# Second line.\n\n" + TARGETS_YAML)
    campaign, targets = load_targets(path)
    targets[0].tier = 3
    write_targets(path, campaign, targets)
    assert path.read_text(encoding="utf-8").startswith("# Schema docs.\n# Second line.\n\n")


def test_unchanged_roster_is_not_rewritten(tmp_path):
    path = _write(tmp_path)
    before = path.stat().st_mtime_ns
    campaign, targets = load_targets(path)
    assert write_targets(path, campaign, targets) is False
    assert path.stat().st_mtime_ns == before


def _assert_round_trips(source: Path, tmp_path):
    copy = tmp_path / "networking_targets.yaml"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    campaign, targets = load_targets(copy)
    assert write_targets(copy, campaign, targets) is False  # nothing to rewrite
    assert copy.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_sample_roster_round_trips_byte_for_byte(tmp_path):
    """A roster must survive a writeback untouched.

    Guards the dump width and the header split: if either drifts, an ordinary
    one-person change would reflow the whole file and bury the real diff. The
    fixture's blurbs are long enough to wrap, which is what pins the width.
    """
    _assert_round_trips(SAMPLE_TARGETS, tmp_path)


@pytest.mark.skipif(not REPO_TARGETS.exists(), reason="private roster not present")
def test_real_roster_round_trips_byte_for_byte(tmp_path):
    """Same check against Paul's real (git-ignored) roster when it is available."""
    _assert_round_trips(REPO_TARGETS, tmp_path)


def test_dump_keeps_the_files_key_order(tmp_path):
    _, campaign, targets, _ = _load(tmp_path)
    body = dump_targets(campaign, targets)
    assert body.index("name: Full Co") < body.index("tier: 2") < body.index("blurb:")
    assert body.index("blurb:") < body.index("people:")

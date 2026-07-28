"""Two-way identity reconciliation between ``networking_targets.yaml`` and storage.

The Networking tab and the targets file are both authoring surfaces for the same
thing — *who to contact at each target company* — so an edit made on either side
has to land on the other. This module is the pure half of that (no I/O, no
Google, fixture-tested); the stage supplies the inputs and performs the writes.

**Who wins.** There is no stored baseline to diff against, and none is needed:
the sheet edit is detected as a delta against storage in the very run it is made
(``rows.apply_sheet_edits`` reports exactly which fields the human just touched).
So, per field:

* the human changed the cell **this run** → storage wins, and the value is
  folded back into the targets file;
* otherwise the file's value wins whenever it is non-empty and differs — that is
  what makes a hand-edit to the YAML (fixing a name, swapping a LinkedIn URL)
  actually propagate to the sheet, instead of being ignored because the field
  was already filled;
* a field the file leaves blank but storage knows is pushed into the file.

Both edited between two runs is resolved sheet-first, on the grounds that the
sheet is the surface being worked daily. Nothing here ever *clears* a value: a
blank on one side is a request to learn from the other, never to erase it.

Company facts (tier/blurb/website/domain/LinkedIn) are not part of this — they
stay one-way, file → storage, and are refreshed by the stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logging_config import get_logger
from .models import Person, make_person_id
from .targets import NetworkingTarget, TargetPerson

log = get_logger(__name__)

# ``Person`` attribute -> the ``TargetPerson`` key holding the same fact.
IDENTITY_FIELDS: dict[str, str] = {
    "name": "name",
    "role": "role",
    "linkedin_url": "linkedin",
    "email": "email",
}


@dataclass
class IdentityMerge:
    """What one reconciliation pass decided."""

    people_changed: list[Person] = field(default_factory=list)
    yaml_changed: bool = False
    pulled: int = 0  # fields the targets file taught storage
    pushed: int = 0  # fields storage/the sheet taught the targets file


def _norm(value: object) -> str:
    return str(value or "").strip()


def _slots(campaign: str, targets: list[NetworkingTarget]) -> dict[str, tuple[NetworkingTarget, int]]:
    """``person_id -> (company, 0-based index into its people list)``.

    Mirrors ``targets.seed_people``'s id scheme exactly, including the
    placeholder a company with nobody listed gets at index 1.
    """
    slots: dict[str, tuple[NetworkingTarget, int]] = {}
    for target in targets:
        count = len(target.people) or 1  # the placeholder occupies slot 1
        for index in range(count):
            slots[make_person_id(campaign, target.name, index + 1)] = (target, index)
    return slots


def merge_identity(
    campaign: str,
    targets: list[NetworkingTarget],
    people: list[Person],
    human_edited: dict[str, set[str]],
) -> IdentityMerge:
    """Reconcile identity fields both ways, mutating ``targets`` and ``people``.

    ``human_edited`` maps person id to the ``Person`` attributes the human just
    changed on the sheet this run. People whose company is no longer in the file
    are left alone — deleting a company from the roster never rewrites its
    stored history.
    """
    result = IdentityMerge()
    slots = _slots(campaign, targets)

    for person in people:
        slot = slots.get(person.person_id)
        if slot is None:
            continue
        target, index = slot
        edited = human_edited.get(person.person_id, frozenset())

        # A company with nobody listed holds one placeholder slot; the first real
        # identity the human supplies materializes the file's ``people`` entry.
        if index >= len(target.people):
            if not person.has_identity():
                continue
            target.people.append(
                TargetPerson(
                    name=_norm(person.name),
                    role=_norm(person.role) or None,
                    linkedin=_norm(person.linkedin_url) or None,
                    email=_norm(person.email) or None,
                )
            )
            result.yaml_changed = True
            result.pushed += 1
            continue

        tp = target.people[index]
        person_dirty = False
        for attr, key in IDENTITY_FIELDS.items():
            stored = _norm(getattr(person, attr))
            listed = _norm(getattr(tp, key))
            if stored == listed:
                continue
            if attr in edited and stored:
                setattr(tp, key, stored)
                result.yaml_changed = True
                result.pushed += 1
            elif listed:
                setattr(person, attr, listed)
                person_dirty = True
                result.pulled += 1
            elif stored:
                setattr(tp, key, stored)
                result.yaml_changed = True
                result.pushed += 1
        if person_dirty:
            result.people_changed.append(person)

    if result.pulled or result.pushed:
        log.info(
            "reconciled networking roster",
            extra={
                "pulled_from_file": result.pulled,
                "pushed_to_file": result.pushed,
                "people_changed": len(result.people_changed),
            },
        )
    return result

"""Phase 6: networking campaigns — the LinkedIn-first, human-executed ladder.

The pipeline seeds people from ``networking_targets.yaml``, drafts every
connect note / follow-up message, runs the escalation timers, and projects the
whole thing onto the sheet's Networking tab; the human sends everything on
LinkedIn by hand and flips each row's Status. LinkedIn is never automated.
"""

from .copy import (
    NetworkingContent,
    NetworkingEmail,
    draft_networking_copy,
    draft_networking_email,
    rank_bullets,
)
from .models import Person, allowed_human_transition, make_person_id
from .sequence import (
    HumanAction,
    awaiting_person_count,
    outstanding_actions,
    plan_due,
)
from .targets import NetworkingTarget, load_targets, seed_people

# NOTE: ``networking.email`` is deliberately NOT re-exported here. It imports
# ``storage`` (for the suppression check), and ``storage.base`` imports
# ``networking.models`` — pulling ``.email`` into this package __init__ closes that
# into a circular import when the process first enters through ``storage``. The one
# caller (the stage) imports it straight from the submodule instead.

__all__ = [
    "HumanAction",
    "NetworkingContent",
    "NetworkingEmail",
    "NetworkingTarget",
    "Person",
    "allowed_human_transition",
    "awaiting_person_count",
    "draft_networking_copy",
    "draft_networking_email",
    "load_targets",
    "make_person_id",
    "outstanding_actions",
    "plan_due",
    "rank_bullets",
    "seed_people",
]

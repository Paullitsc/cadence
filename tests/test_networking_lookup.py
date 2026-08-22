"""Phase 6b — automatic recipient lookup for the escalation email.

The contract under test, in one line: the pipeline may go *find* the address of the
person Paul picked, but it may only *store* one a provider returned verified, and
never one belonging to somebody else.

All offline — providers are exercised through a fake httpx client, everything else
through the pure parse/match helpers and SQLite storage.
"""

from __future__ import annotations

import pytest

from internship_pipeline.config import Settings
from internship_pipeline.networking.lookup import (
    LookupResult,
    eligible_for_lookup,
    resolve_person_emails,
)
from internship_pipeline.networking.models import (
    STATUS_CONNECT_SENT,
    STATUS_EMAIL_DRAFTED,
    STATUS_EMAIL_DUE,
    Person,
)
from internship_pipeline.networking.sequence import outstanding_actions
from internship_pipeline.outreach.contacts import (
    LookupBudget,
    find_person_contact,
    names_match,
    parse_hunter_email_finder,
    split_name,
)
from internship_pipeline.storage import get_storage


# --- fake transport (never hits the network) ---------------------------------- #
class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Returns a canned payload; counts calls so budget spend is observable."""

    def __init__(self, data):
        self._data = data
        self.calls = 0

    def get(self, url, params=None, headers=None):
        self.calls += 1
        self.last_params = params
        return _FakeResp(self._data)

    def post(self, url, json=None, params=None, headers=None):
        self.calls += 1
        self.last_body = json
        return _FakeResp(self._data)

    def close(self):  # the stage closes the client it built
        self.closed = True


def _settings(**over) -> Settings:
    base = dict(
        enable_hunter=True,
        hunter_api_key="k",
        networking_email_escalation_enabled=True,
        networking_email_lookup_enabled=True,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def _person(**over) -> Person:
    fields = dict(
        person_id="test-robotics-co-1",
        company_name="Robotics Co",
        company_domain="roboticsco.com",
        name="Jane Doe",
        role="CTO",
        status=STATUS_EMAIL_DUE,
    )
    fields.update(over)
    return Person(**fields)


def _hunter(email="jane.doe@roboticsco.com", score=95, first="Jane", last="Doe"):
    return {"data": {"email": email, "score": score, "first_name": first, "last_name": last}}


# --------------------------------------------------------------------------- #
# Name matching — the guard that keeps us off the wrong human
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expected,returned",
    [
        ("Jane Doe", "Jane Doe"),
        ("Jane Doe", "jane  doe"),
        ("Jane Doe", "Jane Doe Jr."),  # suffix is noise
        ("Dan Rivera", "Daniel Rivera"),  # given-name prefix
        ("Daniel Rivera", "Dan Rivera"),
        ("Jane Q. Doe", "Jane Doe"),  # middle initial dropped
        ("O'Brien", "Brien O'Brien"),  # mononym matches any token
    ],
)
def test_names_match_accepts_the_same_person(expected, returned):
    assert names_match(expected, returned)


@pytest.mark.parametrize(
    "expected,returned",
    [
        ("Jane Doe", "John Doe"),  # same surname, different human
        ("Jane Doe", "Jane Smith"),
        ("Jane Doe", None),  # unnamed hit is never a match
        ("Jane Doe", ""),
        (None, "Jane Doe"),
        ("Jane Doe", "Recruiting Team"),
    ],
)
def test_names_match_rejects_anyone_else(expected, returned):
    assert not names_match(expected, returned)


def test_split_name_drops_initials_and_suffixes():
    assert split_name("Jane Q. Doe Jr.") == ("jane", "doe")
    assert split_name("Cher") == ("cher", None)
    assert split_name("") == (None, None)


# --------------------------------------------------------------------------- #
# Hunter email-finder parsing
# --------------------------------------------------------------------------- #
def test_parse_hunter_email_finder_verifies_on_high_score():
    contact = parse_hunter_email_finder(_hunter(score=95), expected_name="Jane Doe")
    assert contact.email == "jane.doe@roboticsco.com"
    assert contact.verified is True
    assert contact.source == "hunter"


def test_parse_hunter_email_finder_keeps_low_score_unverified():
    contact = parse_hunter_email_finder(_hunter(score=40), expected_name="Jane Doe")
    assert contact.email == "jane.doe@roboticsco.com"
    assert contact.verified is False
    assert "double-check" in contact.note


def test_parse_hunter_email_finder_discards_a_different_person():
    payload = _hunter(email="bob.smith@roboticsco.com", first="Bob", last="Smith")
    assert parse_hunter_email_finder(payload, expected_name="Jane Doe") is None


def test_parse_hunter_email_finder_handles_empty_and_missing_data():
    assert parse_hunter_email_finder({}, expected_name="Jane Doe") is None
    assert parse_hunter_email_finder({"data": {}}, expected_name="Jane Doe") is None
    assert parse_hunter_email_finder({"data": None}, expected_name="Jane Doe") is None


# --------------------------------------------------------------------------- #
# find_person_contact — orchestration, budget, fallback
# --------------------------------------------------------------------------- #
def test_find_person_contact_returns_the_verified_hit_and_spends_budget():
    client = _FakeClient(_hunter())
    budget = LookupBudget(remaining=2)
    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", domain="roboticsco.com",
        settings=_settings(), client=client, budget=budget,
    )
    assert contact.email == "jane.doe@roboticsco.com"
    assert contact.verified is True
    assert budget.remaining == 1
    assert client.last_params["first_name"] == "jane"


def test_find_person_contact_falls_back_to_a_guess_when_the_name_disagrees():
    client = _FakeClient(_hunter(email="bob@roboticsco.com", first="Bob", last="Smith"))
    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", domain="roboticsco.com",
        settings=_settings(), client=client, budget=LookupBudget(remaining=2),
    )
    assert contact.source == "pattern_guess"
    assert contact.verified is False
    assert contact.email == "jane.doe@roboticsco.com"  # a guess, flagged as such


def test_an_unverified_hunter_hit_still_spends_apollo_and_prefers_its_verified_answer():
    # The networking path discards unverified answers, so a weak Hunter score is
    # worth Apollo's credit rather than ending the search.
    class _Both(_FakeClient):
        def get(self, url, params=None, headers=None):
            self.calls += 1
            return _FakeResp(_hunter(score=20))  # a weak Hunter answer

        def post(self, url, json=None, params=None, headers=None):
            self.calls += 1
            return _FakeResp(
                {"person": {"name": "Jane Doe", "email": "j.doe@roboticsco.com"}}
            )

    client = _Both({})
    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", domain="roboticsco.com",
        settings=_settings(enable_apollo=True, apollo_api_key="a"),
        client=client, budget=LookupBudget(remaining=5),
    )
    assert client.calls == 2  # both providers consulted
    assert contact.verified is True
    assert contact.email == "j.doe@roboticsco.com"


def test_the_unverified_hit_is_returned_when_nothing_better_turns_up():
    # Still surfaced (the digest can show it) — just never stored by lookup.py.
    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", domain="roboticsco.com",
        settings=_settings(), client=_FakeClient(_hunter(score=20)),
        budget=LookupBudget(remaining=5),
    )
    assert contact.source == "hunter"
    assert contact.verified is False


def test_find_person_contact_never_calls_a_provider_without_budget():
    client = _FakeClient(_hunter())
    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", domain="roboticsco.com",
        settings=_settings(), client=client, budget=LookupBudget(remaining=0),
    )
    assert client.calls == 0
    assert contact.source == "pattern_guess"


def test_find_person_contact_is_offline_safe_with_no_client():
    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", settings=Settings(_env_file=None),
        client=None, budget=LookupBudget(remaining=5),
    )
    assert contact.source == "pattern_guess"
    assert contact.verified is False


def test_find_person_contact_survives_a_provider_error():
    class _Boom(_FakeClient):
        def get(self, url, params=None, headers=None):
            raise RuntimeError("hunter is down")

    contact = find_person_contact(
        person_name="Jane Doe", company_name="Robotics Co", domain="roboticsco.com",
        settings=_settings(), client=_Boom({}), budget=LookupBudget(remaining=2),
    )
    assert contact.source == "pattern_guess"


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [STATUS_EMAIL_DUE, STATUS_EMAIL_DRAFTED])
def test_eligible_on_the_stalled_rungs_only(status):
    assert eligible_for_lookup(_person(status=status))


def test_not_eligible_earlier_in_the_ladder():
    # Nothing before email_due needs an address — the LinkedIn steps are by hand.
    assert not eligible_for_lookup(_person(status=STATUS_CONNECT_SENT))


def test_not_eligible_when_an_address_is_already_known():
    assert not eligible_for_lookup(_person(email="jane@roboticsco.com"))
    # A whitespace-only cell is "unknown", not "known" — still worth looking up.
    assert eligible_for_lookup(_person(email="   "))


def test_not_eligible_without_a_name_even_with_a_linkedin_url():
    # has_identity() is satisfied by a URL alone, but every provider path is
    # name-keyed, so such a row has nothing to look up.
    person = _person(name=None, linkedin_url="https://linkedin.com/in/someone")
    assert person.has_identity()
    assert not eligible_for_lookup(person)


# --------------------------------------------------------------------------- #
# resolve_person_emails — the writeback rule
# --------------------------------------------------------------------------- #
def _storage(tmp_path):
    return get_storage(Settings(_env_file=None, storage_backend="sqlite",
                                database_path=str(tmp_path / "pipeline.db")))


def test_a_verified_hit_is_stored_on_the_row(tmp_path):
    storage = _storage(tmp_path)
    person = _person()
    people = [person]
    result = resolve_person_emails(
        people, settings=_settings(), storage=storage,
        client=_FakeClient(_hunter()), budget=LookupBudget(remaining=3),
    )
    assert result == LookupResult(attempted=1, found=1, unverified=0)
    assert person.email == "jane.doe@roboticsco.com"
    assert storage.get_person(person.person_id).email == "jane.doe@roboticsco.com"


def test_an_unverified_hit_is_counted_but_never_stored(tmp_path):
    # The load-bearing rule: eligible_for_email_draft treats any stored address as
    # send-ready, so a low-confidence answer must not reach it.
    storage = _storage(tmp_path)
    person = _person()
    result = resolve_person_emails(
        [person], settings=_settings(), storage=storage,
        client=_FakeClient(_hunter(score=30)), budget=LookupBudget(remaining=3),
    )
    assert result.found == 0
    assert result.unverified == 1
    assert person.email is None
    assert storage.get_person(person.person_id) is None  # nothing written at all


def test_a_guess_is_never_stored(tmp_path):
    storage = _storage(tmp_path)
    person = _person()
    resolve_person_emails(
        [person], settings=_settings(), storage=storage,
        client=_FakeClient({"data": {}}), budget=LookupBudget(remaining=3),
    )
    assert person.email is None


def test_no_client_is_a_no_op(tmp_path):
    person = _person()
    result = resolve_person_emails(
        [person], settings=Settings(_env_file=None), storage=_storage(tmp_path),
        client=None, budget=LookupBudget(remaining=3),
    )
    assert result == LookupResult()
    assert person.email is None


def test_the_budget_caps_the_run_and_leaves_the_rest_manual(tmp_path):
    storage = _storage(tmp_path)
    people = [_person(person_id=f"p-{i}") for i in range(4)]
    client = _FakeClient(_hunter())
    # One unit per person; two units of budget => two rows resolved, two untouched.
    result = resolve_person_emails(
        people, settings=_settings(), storage=storage,
        client=client, budget=LookupBudget(remaining=2),
    )
    assert result.attempted == 2
    assert result.found == 2
    assert [bool(p.email) for p in people] == [True, True, False, False]


def test_rows_with_a_seeded_address_are_skipped_entirely(tmp_path):
    client = _FakeClient(_hunter())
    resolve_person_emails(
        [_person(email="hand@roboticsco.com")], settings=_settings(),
        storage=_storage(tmp_path), client=client, budget=LookupBudget(remaining=3),
    )
    assert client.calls == 0  # no quota spent on a question we already answered


# --------------------------------------------------------------------------- #
# The digest line the human actually reads
# --------------------------------------------------------------------------- #
def test_a_known_address_replaces_the_go_find_it_instruction():
    person = _person(status=STATUS_EMAIL_DRAFTED, email="jane.doe@roboticsco.com")
    (action,) = [a for a in outstanding_actions([person]) if a.kind == "email"]
    assert "Send it to jane.doe@roboticsco.com" in action.instruction
    assert "Find the" not in action.instruction


def test_without_an_address_the_digest_still_shows_a_guess():
    person = _person(status=STATUS_EMAIL_DRAFTED)
    (action,) = [a for a in outstanding_actions([person]) if a.kind == "email"]
    assert "best guess: jane.doe@roboticsco.com" in action.instruction


# --------------------------------------------------------------------------- #
# The stage wiring, end to end (no network: build_client is patched out)
# --------------------------------------------------------------------------- #
TARGETS_YAML = """
campaign: test
companies:
  - name: Alpha Robotics
    tier: 1
    domain: alpharobotics.com
    blurb: Alpha Robotics builds data pipelines for robots in Python.
    people:
      - name: Jane Doe
        role: CTO
        linkedin: https://linkedin.com/in/janedoe
"""
_JANE = "test-alpha-robotics-1"


@pytest.fixture
def stage_settings(tmp_path):
    from pathlib import Path

    targets = tmp_path / "targets.yaml"
    targets.write_text(TARGETS_YAML, encoding="utf-8")
    return Settings(
        _env_file=None,
        storage_backend="sqlite",
        database_path=str(tmp_path / "pipeline.db"),
        networking_targets_file=str(targets),
        master_resume_file=str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml"),
        anthropic_api_key=None,  # deterministic drafting
        tracker_sheets_enabled=False,  # no sheet — storage only
        enable_hunter=True,
        hunter_api_key="k",
        networking_email_escalation_enabled=True,
        networking_email_lookup_enabled=True,
        outreach_from_name="Test Candidate",
        outreach_from_email="candidate@example.com",
        outreach_physical_address="123 Example St, Boston MA",
    )


def _stall_jane(storage):
    """Park Jane at email_due, the rung where a recipient is needed."""
    from datetime import datetime, timezone

    jane = storage.get_person(_JANE)
    jane.status = STATUS_EMAIL_DUE
    jane.status_changed_at = datetime.now(timezone.utc).isoformat()
    storage.save_person(jane)


def _run_stage(settings, monkeypatch, client):
    from internship_pipeline.models import StageContext
    from internship_pipeline.stages import networking as stage

    monkeypatch.setattr(stage, "build_client", lambda timeout: client)
    ctx = StageContext(run_id="test-run", settings=settings)
    return ctx, stage.run(ctx)


def test_stage_resolves_the_address_and_drafts_in_the_same_run(stage_settings, monkeypatch):
    # The ordering guarantee: lookup runs BEFORE drafting, so an address found this
    # run is on the row by the time the escalation email is built.
    client = _FakeClient(_hunter(email="jane.doe@alpharobotics.com"))
    ctx, _ = _run_stage(stage_settings, monkeypatch, client)
    storage = ctx.get_storage()
    _stall_jane(storage)

    _, result = _run_stage(stage_settings, monkeypatch, client)
    assert result.counts["networking_emails_found"] == 1
    assert result.counts["networking_emails_drafted"] == 1
    jane = storage.get_person(_JANE)
    assert jane.email == "jane.doe@alpharobotics.com"
    assert jane.status == STATUS_EMAIL_DRAFTED


def test_stage_leaves_the_row_manual_when_nothing_verified_comes_back(stage_settings, monkeypatch):
    client = _FakeClient(_hunter(score=20))  # a real hit, too weak to trust
    ctx, _ = _run_stage(stage_settings, monkeypatch, client)
    storage = ctx.get_storage()
    _stall_jane(storage)

    _, result = _run_stage(stage_settings, monkeypatch, client)
    assert result.counts["networking_emails_found"] == 0
    assert result.counts["networking_emails_unresolved"] == 1
    assert result.counts["networking_emails_drafted"] == 1  # copy is still written
    assert storage.get_person(_JANE).email is None


def test_stage_never_looks_up_while_the_flag_is_off(stage_settings, monkeypatch):
    s = stage_settings.model_copy(update={"networking_email_lookup_enabled": False})
    client = _FakeClient(_hunter())
    ctx, _ = _run_stage(s, monkeypatch, client)
    _stall_jane(ctx.get_storage())

    _, result = _run_stage(s, monkeypatch, client)
    assert client.calls == 0
    assert result.counts["networking_emails_found"] == 0
    assert ctx.get_storage().get_person(_JANE).email is None


def test_stage_stays_offline_with_no_provider_configured(stage_settings, monkeypatch):
    # Lookup on but no key: no client is ever built, and the stage behaves as before.
    s = stage_settings.model_copy(update={"enable_hunter": False, "hunter_api_key": None})
    built = []

    from internship_pipeline.models import StageContext
    from internship_pipeline.stages import networking as stage

    def _explode(timeout):  # pragma: no cover - must not be reached
        built.append(timeout)
        raise AssertionError("no provider is configured; a client must not be built")

    monkeypatch.setattr(stage, "build_client", _explode)
    ctx = StageContext(run_id="test-run", settings=s)
    stage.run(ctx)
    _stall_jane(ctx.get_storage())
    result = stage.run(StageContext(run_id="test-run-2", settings=s))

    assert built == []
    assert result.counts["networking_emails_drafted"] == 1  # 6b still drafts the copy

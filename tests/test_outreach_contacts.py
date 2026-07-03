"""Contact lookup: pattern-guess honesty, provider parsing, budget/fallback logic.

No test touches the network — provider ``fetch_*`` is exercised through a fake httpx
client; every other test uses the pure ``parse_*`` / guess functions directly.
"""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.outreach.contacts import (
    LookupBudget,
    company_domain_guess,
    domain_from_url,
    find_contact,
    guess_email_pattern,
    parse_apollo_people,
    parse_hunter_domain_search,
)


# --- fake transport (never hits the network) ---------------------------------- #
class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data):
        self._data = data
        self.calls = 0

    def get(self, url, params=None, headers=None):
        self.calls += 1
        return _FakeResp(self._data)

    def post(self, url, json=None, params=None, headers=None):
        self.calls += 1
        return _FakeResp(self._data)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


# --- domain / pattern guessing ------------------------------------------------ #
def test_domain_from_url_skips_ats_hosts_and_reads_company_hosts():
    assert domain_from_url("https://acme.com/careers/123") == "acme.com"
    assert domain_from_url("https://jobs.lever.co/acme/123") is None  # ATS, not the company
    assert domain_from_url("https://boards.greenhouse.io/acme") is None
    assert domain_from_url(None) is None


def test_company_domain_guess_strips_legal_suffixes():
    assert company_domain_guess("Acme Labs, Inc.") == "acmelabs.com"
    assert company_domain_guess("Foo Bar LLC") == "foobar.com"
    assert company_domain_guess("") is None


def test_guess_email_pattern_is_never_presented_as_certain():
    c = guess_email_pattern("Acme Labs")
    assert c.source == "pattern_guess"
    assert c.verified is False
    assert c.confidence is None
    assert c.email is None  # we never invent a specific person
    assert c.pattern == "{first}.{last}@acmelabs.com"
    assert "guess" in (c.note or "").lower()


def test_guess_email_pattern_fills_in_a_supplied_name():
    c = guess_email_pattern("Acme", domain="acme.com", first="Ada", last="Lovelace")
    assert c.email == "ada.lovelace@acme.com"
    assert c.verified is False  # still a guess even with an address


# --- Hunter parsing ----------------------------------------------------------- #
def test_parse_hunter_prefers_recruiting_contact_over_higher_confidence_other():
    payload = {"data": {"domain": "acme.com", "pattern": "{first}", "emails": [
        {"value": "sales@acme.com", "department": "sales", "confidence": 95},
        {"value": "jobs@acme.com", "position": "Technical Recruiter", "confidence": 70},
    ]}}
    c = parse_hunter_domain_search(payload)
    assert c is not None
    assert c.email == "jobs@acme.com"  # recruiter wins despite lower confidence
    assert c.verified is False  # 70 < 80 → not treated as verified
    assert c.note  # carries a "double-check" caution


def test_parse_hunter_high_confidence_is_verified():
    payload = {"data": {"domain": "acme.com", "emails": [
        {"value": "talent@acme.com", "department": "people", "confidence": 92},
    ]}}
    c = parse_hunter_domain_search(payload)
    assert c is not None and c.verified is True and c.note is None


def test_parse_hunter_pattern_only_returns_unverified_contact():
    payload = {"data": {"domain": "acme.com", "pattern": "{first}.{last}", "emails": []}}
    c = parse_hunter_domain_search(payload)
    assert c is not None and c.email is None and c.source == "hunter" and c.verified is False


def test_parse_hunter_empty_returns_none():
    assert parse_hunter_domain_search({"data": {"emails": []}}) is None
    assert parse_hunter_domain_search({}) is None


# --- Apollo parsing ----------------------------------------------------------- #
def test_parse_apollo_masked_email_stays_unverified():
    payload = {"person": {"email": "email_not_unlocked@acme.com", "first_name": "Ada",
                          "last_name": "Lovelace", "title": "Recruiter"}}
    c = parse_apollo_people(payload)
    assert c is not None
    assert c.email is None  # the mask is never presented as a real address
    assert c.verified is False
    assert c.name == "Ada Lovelace"
    assert "lock" in (c.note or "").lower() or "reveal" in (c.note or "").lower()


def test_parse_apollo_real_email_is_verified_and_reads_people_list():
    c = parse_apollo_people({"people": [{"email": "ada@acme.com", "name": "Ada Lovelace"}]})
    assert c is not None and c.email == "ada@acme.com" and c.verified is True


def test_parse_apollo_no_person_returns_none():
    assert parse_apollo_people({}) is None


# --- orchestration: budget + graceful fallback -------------------------------- #
def test_find_contact_falls_back_to_guess_when_paid_disallowed():
    c = find_contact(company_name="Acme Labs", url="https://jobs.lever.co/acme/1",
                     settings=_settings(), client=None, budget=LookupBudget(5), allow_paid=False)
    assert c.source == "pattern_guess" and c.verified is False


def test_find_contact_spends_budget_on_a_hunter_hit():
    client = _FakeClient({"data": {"domain": "acme.com", "emails": [
        {"value": "talent@acme.com", "department": "people", "confidence": 90}]}})
    budget = LookupBudget(2)
    c = find_contact(
        company_name="Acme", url="https://acme.com/careers/1",
        settings=_settings(enable_hunter=True, hunter_api_key="k"),
        client=client, budget=budget, allow_paid=True,
    )
    assert c.source == "hunter" and c.email == "talent@acme.com"
    assert client.calls == 1 and budget.remaining == 1  # one unit spent


def test_find_contact_respects_exhausted_budget_without_calling_provider():
    client = _FakeClient({"data": {}})
    budget = LookupBudget(0)
    c = find_contact(
        company_name="Acme", url="https://acme.com/careers/1",
        settings=_settings(enable_hunter=True, hunter_api_key="k"),
        client=client, budget=budget, allow_paid=True,
    )
    assert client.calls == 0  # never billed once the cap is hit
    assert c.source == "pattern_guess"


def test_lookup_budget_never_goes_negative():
    b = LookupBudget(1)
    b.spend()
    b.spend()
    assert b.remaining == 0 and b.can_spend() is False

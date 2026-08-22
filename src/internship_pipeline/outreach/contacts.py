"""Contact lookup for cold outreach (Phase 3) and networking escalations (Phase 6b).

Two paid providers behind feature flags — **Hunter.io** and **Apollo.io** — plus a
free, always-available fallback that GUESSES the company email pattern
(``first.last@company.com``) **without ever claiming certainty**. A guessed contact
is returned with ``verified=False``, ``confidence=None`` and an explicit note so it
can never be mistaken for a confirmed address.

Two orchestrators, because the two callers ask different questions:

* ``find_contact`` — *"anyone worth emailing at this company"* (Phase 3 cold apply).
  Hunter domain-search ranks recruiting/talent addresses first; any hit will do.
* ``find_person_contact`` — *"the address of THIS named person"* (Phase 6b). Paul
  picked the human; emailing a different one would send a draft addressed to someone
  else. So it queries the name-targeted endpoints and every provider hit must clear
  ``names_match`` before it is accepted — a mismatch degrades to the pattern guess.

Free tiers are small (Hunter ~25-50 searches/mo, Apollo ~100 credits/mo), so paid
lookups are: (a) gated behind ``ENABLE_HUNTER`` / ``ENABLE_APOLLO`` + a key,
(b) reserved for high-priority roles (config), and (c) hard-capped per run by a
``LookupBudget``. Everything else uses the free guess.

Split mirrors ``sourcing/``: ``parse_*`` are pure (fixture-testable, no I/O);
``fetch_*`` do the HTTP through the shared retrying client.

    # VERIFY: the exact provider response field names / endpoints below are taken
    # from the Hunter v2 and Apollo v1 API docs, NOT confirmed against a live call in
    # this environment. Parsing is defensive (a missing/renamed field degrades to
    # "no contact" rather than crashing). Confirm against a real key before relying on
    # provider hits — see ACTIONS_FOR_PAUL.md. Paul: tell me if either contract differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from ..config import Settings
from ..logging_config import get_logger
from ..sourcing.http import get_json, post_json

log = get_logger(__name__)

# Hosts that belong to an ATS/aggregator, NOT the hiring company — so a job URL on
# one of these tells us nothing about the company's own email domain.
_NON_COMPANY_HOSTS: frozenset[str] = frozenset(
    """
    greenhouse.io boards.greenhouse.io job-boards.greenhouse.io lever.co jobs.lever.co
    ashbyhq.com jobs.ashbyhq.com simplify.jobs linkedin.com indeed.com glassdoor.com
    google.com myworkdayjobs.com workday.com icims.com smartrecruiters.com workable.com
    recruitee.com bamboohr.com jobvite.com github.com githubusercontent.com
    """.split()
)

# Common legal suffixes stripped when guessing a domain from a company name.
_LEGAL_SUFFIXES: frozenset[str] = frozenset(
    "inc incorporated llc llp ltd limited corp corporation co company gmbh ag plc sa".split()
)


class Contact(BaseModel):
    """A (possibly guessed) outreach recipient.

    ``verified`` and ``confidence`` are the honesty signals: a pattern guess always
    has ``verified=False`` and ``confidence=None``. ``pattern`` holds the inferred
    address shape (e.g. ``{first}.{last}@acme.com``) so a human can complete it.
    """

    email: Optional[str] = None
    name: Optional[str] = None
    title: Optional[str] = None  # recipient role/position
    source: str = "none"  # hunter | apollo | pattern_guess | none
    confidence: Optional[int] = None  # provider score 0-100 (None when guessed)
    verified: bool = False
    pattern: Optional[str] = None
    note: Optional[str] = None


@dataclass
class LookupBudget:
    """Hard cap on billable provider calls for one run (free-tier guard)."""

    remaining: int

    def can_spend(self) -> bool:
        return self.remaining > 0

    def spend(self) -> None:
        self.remaining = max(0, self.remaining - 1)


# --------------------------------------------------------------------------- #
# Domain / pattern guessing (pure, free, never "certain")
# --------------------------------------------------------------------------- #
def _registrable_domain(host: str) -> Optional[str]:
    host = (host or "").lower().split(":", 1)[0].strip().removeprefix("www.")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    # Naive registrable domain = last two labels (good enough; not a PSL parser).
    return ".".join(labels[-2:])


def domain_from_url(url: Optional[str]) -> Optional[str]:
    """Best-effort company domain from a job URL, skipping ATS/aggregator hosts."""
    if not url:
        return None
    host = urlsplit(url).netloc.lower()
    if not host:
        return None
    reg = _registrable_domain(host)
    if reg is None or host in _NON_COMPANY_HOSTS or reg in _NON_COMPANY_HOSTS:
        return None
    return reg


def company_domain_guess(company_name: str) -> Optional[str]:
    """Guess a company's domain from its name (e.g. 'Acme Labs' -> 'acmelabs.com').

    A GUESS, not a lookup. Returns None if nothing sensible can be formed.
    """
    words = [w for w in "".join(c if c.isalnum() or c.isspace() else " " for c in company_name).split()]
    words = [w.lower() for w in words if w.lower() not in _LEGAL_SUFFIXES]
    if not words:
        return None
    return "".join(words) + ".com"


# --------------------------------------------------------------------------- #
# Name matching (pure) — the guard on person-targeted lookups
# --------------------------------------------------------------------------- #
# Dropped before comparing: they carry no identity and providers are inconsistent
# about including them.
_NAME_NOISE: frozenset[str] = frozenset(
    "jr sr ii iii iv phd md mba dr mr mrs ms prof".split()
)
_NAME_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _name_tokens(name: Optional[str]) -> list[str]:
    """Lowercased identity tokens: no punctuation, no initials, no suffixes."""
    raw = _NAME_SPLIT_RE.split((name or "").lower())
    return [t for t in raw if len(t) > 1 and t not in _NAME_NOISE]


def _first_matches(expected: str, returned: str) -> bool:
    """Equal, or one is a prefix of the other — covers Dan/Daniel, Chris/Christopher.

    Deliberately not a nickname table: an unrecognized diminutive (Mike/Michael)
    just falls back to the pattern guess, which is the safe direction to fail.
    """
    return expected == returned or expected.startswith(returned) or returned.startswith(expected)


def names_match(expected: Optional[str], returned: Optional[str]) -> bool:
    """True when a provider's contact plausibly IS the person we asked about.

    Surname must match exactly and the given name must match under
    ``_first_matches``. A missing/unusable name on either side is **never** a
    match: an unnamed provider hit is exactly the case where we would otherwise
    email the wrong human. A mononym expectation ("Cher") matches any returned
    token, since there is no surname to anchor on.
    """
    want = _name_tokens(expected)
    got = _name_tokens(returned)
    if not want or not got:
        return False
    if len(want) == 1:
        return any(_first_matches(want[0], token) for token in got)
    return want[-1] == got[-1] and _first_matches(want[0], got[0])


def split_name(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``"Ada Lovelace"`` -> ``("Ada", "Lovelace")``; a mononym has no surname."""
    tokens = _name_tokens(name)
    if not tokens:
        return None, None
    return tokens[0], (tokens[-1] if len(tokens) > 1 else None)


def guess_email_pattern(
    company_name: str,
    *,
    domain: Optional[str] = None,
    first: Optional[str] = None,
    last: Optional[str] = None,
) -> Contact:
    """Free fallback: infer the company email PATTERN without claiming certainty.

    If a first/last name is supplied we fill the pattern in; otherwise we return the
    pattern shape only (``email`` stays None — we never invent a specific person).
    Always ``verified=False``, ``confidence=None``, with a note saying it is a guess.
    """
    domain = domain or company_domain_guess(company_name)
    pattern = f"{{first}}.{{last}}@{domain}" if domain else None
    email = None
    if domain and first and last:
        email = f"{first}.{last}@{domain}".lower()
    note = (
        "No verified contact found — this is a GUESS of the company email pattern, "
        "not a confirmed address. Verify a real recipient (careers page / LinkedIn) "
        "before sending."
    )
    return Contact(
        email=email,
        name=(f"{first} {last}".strip() if first or last else None),
        source="pattern_guess",
        confidence=None,
        verified=False,
        pattern=pattern,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Hunter.io (pure parse + network fetch)
# --------------------------------------------------------------------------- #
def parse_hunter_domain_search(payload: dict[str, Any]) -> Optional[Contact]:
    """Pick the best contact from a Hunter domain-search response.

    Prefers a recruiting/people email, else the highest-confidence personal email,
    else surfaces just the pattern (still unverified). Returns None if unusable.
    """
    # VERIFY: Hunter v2 domain-search shape: {"data": {"pattern", "emails": [
    #   {"value","type","confidence","first_name","last_name","position","department"}]}}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    pattern = data.get("pattern")
    pattern_str = f"{{{pattern}}}@{data.get('domain')}" if pattern and data.get("domain") else None
    emails = data.get("emails") or []

    def _rank(e: dict) -> tuple[int, int]:
        dept = (e.get("department") or "").lower()
        pos = (e.get("position") or "").lower()
        prefer = any(k in dept or k in pos for k in ("recruit", "talent", "people", "hr", "hiring"))
        return (1 if prefer else 0, int(e.get("confidence") or 0))

    best = None
    for e in emails:
        if isinstance(e, dict) and e.get("value"):
            if best is None or _rank(e) > _rank(best):
                best = e

    if best is None:
        if pattern_str is None:
            return None
        return Contact(
            source="hunter", verified=False, confidence=None, pattern=pattern_str,
            note="Hunter returned an email pattern but no specific address — verify before sending.",
        )
    name = " ".join(p for p in (best.get("first_name"), best.get("last_name")) if p) or None
    confidence = int(best.get("confidence")) if best.get("confidence") is not None else None
    return Contact(
        email=best.get("value"),
        name=name,
        title=best.get("position"),
        source="hunter",
        confidence=confidence,
        # A Hunter hit is a real, discovered address; treat as verified only if the
        # provider is reasonably confident. Low confidence stays "unverified".
        verified=bool(confidence is not None and confidence >= 80),
        pattern=pattern_str,
        note=None if (confidence and confidence >= 80) else
        "Low-confidence Hunter result — double-check the address before sending.",
    )


def fetch_hunter(client: httpx.Client, *, domain: str, api_key: str, max_retries: int = 3) -> Optional[Contact]:
    """Call Hunter domain-search for ``domain``. Returns a Contact or None."""
    # VERIFY: endpoint per Hunter API v2 docs (api.hunter.io/v2/domain-search).
    payload = get_json(
        client,
        "https://api.hunter.io/v2/domain-search",
        params={"domain": domain, "api_key": api_key, "limit": "10"},
        max_retries=max_retries,
    )
    return parse_hunter_domain_search(payload)


def parse_hunter_email_finder(payload: dict[str, Any], *, expected_name: Optional[str]) -> Optional[Contact]:
    """Read a Hunter email-finder response for ONE named person.

    Unlike domain-search this is already name-targeted, but the response is still
    checked against ``expected_name`` when Hunter echoes one back — a provider that
    silently answers with a different human must not produce a "verified" contact.
    Returns None when there is no address to use.
    """
    # VERIFY: Hunter v2 email-finder shape: {"data": {"email","score","domain",
    #   "first_name","last_name","position","verification":{"status","date"}}}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    email = data.get("email")
    if not email:
        return None
    name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p) or None
    # Hunter omitting the name is fine — the query itself pinned the person. A name
    # that comes back and disagrees is not.
    if name and expected_name and not names_match(expected_name, name):
        log.info(
            "hunter email-finder returned a different person; discarding",
            extra={"expected": expected_name, "returned": name},
        )
        return None
    score = int(data["score"]) if data.get("score") is not None else None
    return Contact(
        email=email,
        name=name or expected_name,
        title=data.get("position"),
        source="hunter",
        confidence=score,
        verified=bool(score is not None and score >= 80),
        note=None if (score and score >= 80) else
        "Low-confidence Hunter result — double-check the address before sending.",
    )


def fetch_hunter_email_finder(
    client: httpx.Client,
    *,
    domain: str,
    first_name: str,
    last_name: str,
    api_key: str,
    expected_name: Optional[str] = None,
    max_retries: int = 3,
) -> Optional[Contact]:
    """Ask Hunter for one named person's address at ``domain``."""
    # VERIFY: endpoint per Hunter API v2 docs (api.hunter.io/v2/email-finder).
    payload = get_json(
        client,
        "https://api.hunter.io/v2/email-finder",
        params={
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
            "api_key": api_key,
        },
        max_retries=max_retries,
    )
    return parse_hunter_email_finder(payload, expected_name=expected_name)


# --------------------------------------------------------------------------- #
# Apollo.io (pure parse + network fetch)
# --------------------------------------------------------------------------- #
def parse_apollo_people(payload: dict[str, Any]) -> Optional[Contact]:
    """Pick a contact from an Apollo people match/search response.

    Apollo often returns a MASKED placeholder (``email_not_unlocked@domain.com``)
    until a credit is spent to reveal it; we detect that and keep the contact
    unverified with a note rather than presenting the mask as a real address.
    """
    # VERIFY: Apollo v1 response shape assumed as {"person": {...}} or
    # {"people": [{...}]} with fields first_name/last_name/name/title/email/organization.
    data = payload if isinstance(payload, dict) else {}
    person = data.get("person")
    if not isinstance(person, dict):
        people = data.get("people") or data.get("matches") or []
        person = people[0] if people and isinstance(people[0], dict) else None
    if not isinstance(person, dict):
        return None

    email = person.get("email")
    masked = bool(email and "email_not_unlocked" in str(email).lower())
    name = person.get("name") or " ".join(
        p for p in (person.get("first_name"), person.get("last_name")) if p
    ) or None
    if masked or not email:
        return Contact(
            name=name, title=person.get("title"), source="apollo", confidence=None,
            verified=False, email=None,
            note="Apollo found a person but the email is locked/unrevealed — "
            "reveal in Apollo (spends a credit) or verify manually before sending.",
        )
    return Contact(
        email=email,
        name=name,
        title=person.get("title"),
        source="apollo",
        confidence=None,  # Apollo does not return a comparable 0-100 score here
        verified=True,
        note=None,
    )


def fetch_apollo(
    client: httpx.Client,
    *,
    company_name: str,
    domain: Optional[str],
    api_key: str,
    person_name: Optional[str] = None,
    max_retries: int = 3,
) -> Optional[Contact]:
    """Call Apollo people match for a company, optionally for one named person.

    With ``person_name`` the match is narrowed to that human (Phase 6b); without
    it Apollo picks whoever it considers the best match at the company (Phase 3).
    """
    # VERIFY: endpoint + auth for Apollo. Docs show POST api.apollo.io/v1/people/match
    # with the key in an "X-Api-Key" header (some accounts use ?api_key=). Confirm both.
    body: dict[str, Any] = {"organization_name": company_name, "reveal_personal_emails": False}
    if domain:
        body["domain"] = domain
    if person_name:
        first, last = split_name(person_name)
        body["name"] = person_name
        if first:
            body["first_name"] = first
        if last:
            body["last_name"] = last
    payload = post_json(
        client,
        "https://api.apollo.io/v1/people/match",
        json=body,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        max_retries=max_retries,
    )
    return parse_apollo_people(payload)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def find_contact(
    *,
    company_name: str,
    url: Optional[str],
    settings: Settings,
    client: Optional[httpx.Client],
    budget: LookupBudget,
    allow_paid: bool,
) -> Contact:
    """Resolve one outreach contact.

    Tries the enabled paid providers (Hunter, then Apollo) only when ``allow_paid``
    and the ``budget`` still has room; each real call spends one unit of budget
    whether or not it finds an address. Always falls back to the free, honest pattern
    guess — so this never raises for lack of a provider and never claims certainty.
    """
    domain = domain_from_url(url) or company_domain_guess(company_name)

    if allow_paid and client is not None:
        if settings.enable_hunter and settings.hunter_api_key and domain and budget.can_spend():
            budget.spend()
            try:
                contact = fetch_hunter(
                    client, domain=domain, api_key=settings.hunter_api_key,
                    max_retries=settings.http_max_retries,
                )
                if contact and contact.email:
                    return contact
            except Exception as exc:  # skip-on-error: fall through to the next option
                log.warning("hunter lookup failed; falling back", extra={"company": company_name, "error": repr(exc)})

        if settings.enable_apollo and settings.apollo_api_key and budget.can_spend():
            budget.spend()
            try:
                contact = fetch_apollo(
                    client, company_name=company_name, domain=domain,
                    api_key=settings.apollo_api_key, max_retries=settings.http_max_retries,
                )
                if contact and contact.email:
                    return contact
            except Exception as exc:
                log.warning("apollo lookup failed; falling back", extra={"company": company_name, "error": repr(exc)})

    return guess_email_pattern(company_name, domain=domain)


def find_person_contact(
    *,
    person_name: str,
    company_name: str,
    settings: Settings,
    client: Optional[httpx.Client],
    budget: LookupBudget,
    domain: Optional[str] = None,
    allow_paid: bool = True,
) -> Contact:
    """Resolve the address of ONE named person at a company (Phase 6b).

    The person-targeted sibling of ``find_contact``: Hunter's email-finder first
    (it takes the name directly), then Apollo's people-match narrowed to that name.
    Every hit must clear ``names_match`` — a provider answering with a different
    human is discarded rather than returned, because the drafted email already
    greets the person Paul chose.

    Falls back to the free pattern guess exactly like ``find_contact``, so this
    never raises for lack of a provider and never claims certainty. Each real
    provider call spends one unit of ``budget`` whether or not it finds an address.

    Unlike ``find_contact``, an *unverified* hit does not end the search: the only
    caller stores verified addresses and nothing else, so a low-confidence Hunter
    score is worth spending Apollo's credit on rather than returning something that
    will be thrown away. The best unverified answer is kept and returned if Apollo
    does no better.
    """
    domain = domain or company_domain_guess(company_name)
    first, last = split_name(person_name)
    fallback: Optional[Contact] = None  # best unverified answer seen so far

    if allow_paid and client is not None:
        if (
            settings.enable_hunter and settings.hunter_api_key and domain
            and first and last and budget.can_spend()
        ):
            budget.spend()
            try:
                contact = fetch_hunter_email_finder(
                    client, domain=domain, first_name=first, last_name=last,
                    api_key=settings.hunter_api_key, expected_name=person_name,
                    max_retries=settings.http_max_retries,
                )
                if contact and contact.email:
                    if contact.verified:
                        return contact
                    fallback = contact
            except Exception as exc:  # skip-on-error: fall through to the next option
                log.warning(
                    "hunter email-finder failed; falling back",
                    extra={"company": company_name, "error": repr(exc)},
                )

        if settings.enable_apollo and settings.apollo_api_key and budget.can_spend():
            budget.spend()
            try:
                contact = fetch_apollo(
                    client, company_name=company_name, domain=domain,
                    api_key=settings.apollo_api_key, person_name=person_name,
                    max_retries=settings.http_max_retries,
                )
                # Apollo matches fuzzily on the organization, so the name guard
                # matters more here than it does for Hunter's targeted endpoint.
                if contact and contact.email:
                    if not names_match(person_name, contact.name):
                        log.info(
                            "apollo returned a different person; discarding",
                            extra={"expected": person_name, "returned": contact.name},
                        )
                    elif contact.verified:
                        return contact
                    elif fallback is None:
                        fallback = contact
            except Exception as exc:
                log.warning(
                    "apollo person lookup failed; falling back",
                    extra={"company": company_name, "error": repr(exc)},
                )

    if fallback is not None:
        return fallback
    return guess_email_pattern(company_name, domain=domain, first=first, last=last)

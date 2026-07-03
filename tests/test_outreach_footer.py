"""CAN-SPAM footer construction + the merged (DB + seed file) suppression rule."""

from __future__ import annotations

from internship_pipeline.config import Settings
from internship_pipeline.outreach.footer import (
    OPT_OUT_MARKER,
    build_email_body,
    build_footer,
    has_footer,
    is_address_placeholder,
)
from internship_pipeline.outreach.suppress import is_suppressed, load_suppression_seed
from internship_pipeline.storage.base import suppression_matches
from internship_pipeline.storage.sqlite_store import SQLiteStore


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


# --- footer ------------------------------------------------------------------- #
def test_build_footer_carries_identity_address_and_opt_out():
    s = _settings(outreach_from_name="Ada Lovelace", outreach_from_email="ada@x.com",
                  outreach_physical_address="1 Real St, Townsville")
    footer = build_footer(s)
    assert "Ada Lovelace" in footer
    assert "ada@x.com" in footer
    assert "1 Real St, Townsville" in footer
    assert OPT_OUT_MARKER in footer


def test_build_email_body_appends_footer_and_keeps_copy():
    s = _settings(outreach_from_name="Ada", outreach_physical_address="1 Real St")
    body = build_email_body("Hi there, quick note about the role.", s)
    assert "Hi there, quick note about the role." in body
    assert has_footer(body) is True


def test_is_address_placeholder_defaults_true_until_set():
    assert is_address_placeholder(_settings()) is True  # ships as REPLACE_ME
    assert is_address_placeholder(_settings(outreach_physical_address="1 Real St")) is False


def test_has_footer_false_for_bare_body():
    assert has_footer("just some copy, no footer") is False


# --- suppression rule --------------------------------------------------------- #
def test_suppression_matches_email_and_domain_case_insensitively():
    entries = ["Blocked@Acme.com", "competitor.com"]
    assert suppression_matches("blocked@acme.com", entries) is True   # exact
    assert suppression_matches("anyone@competitor.com", entries) is True  # whole domain
    assert suppression_matches("ok@fine.com", entries) is False


def test_load_suppression_seed_reads_file_and_skips_comments(tmp_path):
    seed = tmp_path / "suppress.txt"
    seed.write_text("# do not contact\nBlocked@Acme.com\n\ncompetitor.com  # rival\n")
    entries = load_suppression_seed(_settings(outreach_suppression_file=str(seed)))
    assert entries == ["blocked@acme.com", "competitor.com"]


def test_load_suppression_seed_empty_when_unset():
    assert load_suppression_seed(_settings()) == []


def test_is_suppressed_merges_db_and_seed_file(tmp_path):
    store = SQLiteStore(str(tmp_path / "p.db"))
    store.add_suppression("dbblocked.com")
    seed = tmp_path / "seed.txt"
    seed.write_text("seedblocked@acme.com\n")
    s = _settings(outreach_suppression_file=str(seed))

    assert is_suppressed("anyone@dbblocked.com", store, s) is True   # from the DB
    assert is_suppressed("seedblocked@acme.com", store, s) is True   # from the seed file
    assert is_suppressed("fine@ok.com", store, s) is False
    assert is_suppressed("", store, s) is False

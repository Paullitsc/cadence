"""Dual-trigger logic: favorability (target company / recency proxy) + the AND rule."""

from __future__ import annotations

from datetime import datetime, timezone

from internship_pipeline.config import Settings
from internship_pipeline.models import Job
from internship_pipeline.triggers import favorability, is_dual_trigger, posted_within_days

NOW = datetime(2026, 7, 2, tzinfo=timezone.utc)


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def _job(company="Other Co", date_posted=None) -> Job:
    return Job(company_name=company, title="Backend Intern", url="https://x/1", date_posted=date_posted)


def test_target_company_is_favorable():
    f = favorability(_job("Acme Labs"), _s(target_companies="acme labs, foo"), now=NOW)
    assert f.favorable is True and "target" in f.reason


def test_recently_posted_is_favorable_proxy_for_deadline_soon():
    f = favorability(_job(date_posted="2026-06-30"), _s(favorable_recent_days=7), now=NOW)
    assert f.favorable is True and "posted within" in f.reason


def test_old_posting_is_not_favorable():
    assert favorability(_job(date_posted="2026-06-01"), _s(favorable_recent_days=7), now=NOW).favorable is False


def test_missing_date_is_not_favorable():
    assert favorability(_job(date_posted=None), _s(favorable_recent_days=7), now=NOW).favorable is False


def test_epoch_seconds_date_parses():
    ts = str(int(datetime(2026, 6, 30, tzinfo=timezone.utc).timestamp()))
    assert posted_within_days(_job(date_posted=ts), 7, now=NOW) is True


def test_dual_trigger_requires_high_fit_AND_favorable():
    s = _s(high_priority_threshold=0.5)
    assert is_dual_trigger(0.6, True, s) is True     # both
    assert is_dual_trigger(0.6, False, s) is False   # high-fit but not favorable
    assert is_dual_trigger(0.4, True, s) is False    # favorable but not high-fit

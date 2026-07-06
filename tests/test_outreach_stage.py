"""The draft_outreach stage end-to-end, fully offline (pattern-guess + deterministic).

Confirms the stage produces exactly one email + one LinkedIn row per prepared job,
that the email carries the CAN-SPAM footer while the LinkedIn note does not, that an
offline run spends zero paid lookups, and that a suppressed contact is flagged.
"""

from __future__ import annotations

from pathlib import Path

from internship_pipeline.config import Settings
from internship_pipeline.models import Application, Job, StageContext, make_outreach_id
from internship_pipeline.outreach.contacts import Contact
from internship_pipeline.outreach.footer import has_footer
from internship_pipeline.resume.loader import all_bullets, load_master_resume
from internship_pipeline.stages import draft_outreach
from internship_pipeline.stages.match_and_slice import PreparedApplication
from internship_pipeline.storage.sqlite_store import SQLiteStore

FIXTURE = str(Path(__file__).parent / "fixtures" / "master_resume_sample.yaml")

JOB = Job(company_name="Acme Labs", title="Backend Intern", url="https://acme.com/jobs/1")


def _ctx(tmp_path, resume, *, human_review=False, dual_trigger=True, **settings_over):
    settings = Settings(
        _env_file=None, storage_backend="sqlite",
        database_path=str(tmp_path / "p.db"), master_resume_file=FIXTURE,
        **settings_over,
    )
    app = Application(dedupe_key=JOB.dedupe_key(), company_name=JOB.company_name,
                      title=JOB.title, url=JOB.url, human_review=human_review)
    # Phase 4: outreach is drafted only for dual-trigger roles.
    prepared = PreparedApplication(job=JOB, keywords=["python", "kafka"], app=app,
                                   top_bullets=all_bullets(resume),
                                   favorable=dual_trigger, dual_trigger=dual_trigger)
    ctx = StageContext(run_id="test-run", settings=settings)
    ctx.data["prepared"] = [prepared]
    ctx.data["resume"] = resume
    return ctx, settings


def test_stage_drafts_one_email_and_one_linkedin_per_job(tmp_path):
    resume = load_master_resume(FIXTURE)
    ctx, settings = _ctx(tmp_path, resume)

    result = draft_outreach.run(ctx)

    assert result.counts["outreach_drafted"] == 2
    assert result.counts["email_drafts"] == 1 and result.counts["linkedin_drafts"] == 1
    assert result.counts["paid_lookups_used"] == 0  # offline: no billable lookups
    assert result.counts["guessed_contacts"] == 1   # pattern guess, not verified
    assert len(ctx.data["outreach"]) == 2

    store = SQLiteStore(str(tmp_path / "p.db"))
    email = store.get_outreach(make_outreach_id(JOB.dedupe_key(), "email"))
    linkedin = store.get_outreach(make_outreach_id(JOB.dedupe_key(), "linkedin"))
    assert email is not None and linkedin is not None
    assert email.channel == "email" and has_footer(email.body) is True
    assert email.contact_source == "pattern_guess" and email.contact_verified is False
    assert email.status == "pending_review"
    # LinkedIn is draft-only: no CAN-SPAM footer, references the company.
    assert linkedin.channel == "linkedin" and has_footer(linkedin.body) is False
    assert "Acme Labs" in linkedin.body


def test_stage_no_prepared_is_a_noop(tmp_path):
    settings = Settings(_env_file=None, storage_backend="sqlite",
                        database_path=str(tmp_path / "p.db"), master_resume_file=FIXTURE)
    ctx = StageContext(run_id="r", settings=settings)
    result = draft_outreach.run(ctx)
    assert result.counts["outreach_drafted"] == 0


def test_stage_skips_non_dual_trigger_roles(tmp_path):
    """A prepared role that isn't dual-trigger gets an application but NO outreach."""
    resume = load_master_resume(FIXTURE)
    ctx, _ = _ctx(tmp_path, resume, dual_trigger=False)
    result = draft_outreach.run(ctx)
    assert result.counts["outreach_drafted"] == 0
    assert result.counts["dual_trigger_roles"] == 0


def test_stage_flags_a_suppressed_contact(tmp_path, monkeypatch):
    resume = load_master_resume(FIXTURE)
    ctx, settings = _ctx(tmp_path, resume)

    # Force a resolvable, real-looking contact so suppression has an email to match.
    monkeypatch.setattr(
        draft_outreach, "find_contact",
        lambda **kw: Contact(email="recruiter@acme.com", name="Ada Lovelace",
                             source="hunter", verified=True),
    )
    # Put that contact's domain on the do-not-contact list before the run.
    SQLiteStore(str(tmp_path / "p.db")).add_suppression("acme.com")

    result = draft_outreach.run(ctx)
    assert result.counts["suppressed"] == 1

    email = SQLiteStore(str(tmp_path / "p.db")).get_outreach(make_outreach_id(JOB.dedupe_key(), "email"))
    assert email.suppressed is True and email.status == "suppressed"

"""v2.3 quality gates: readability rules, placeholder hard block, per-prospect
subjects, owner-name visibility, draft purge.

The first test pins the exact draft that motivated this release: a degenerate
run-on that passed every pre-v2.3 check and reached the outbox.
"""
import asyncio

import pytest

from db.models import (Connection, ConnectionKind, Prospect, ProspectStatus,
                       Touch, TouchStatus, TouchType, VerificationLevel)
from engine.campaign import CampaignNotReady, assert_campaign_ready, get_campaign
from engine.config import get_settings
from engine.writer import (SUBJECT_PATTERNS, check_style, render_email_template,
                           render_footer, template_context)


def _sig() -> str:
    from engine.campaign import get_campaign

    return get_campaign().signature

# The actual body a DRY_RUN QA pass found queued in /outbox. 58 words, one full
# stop, zero structure. It satisfied every rule check_style had at the time.
DEGENERATE = (
    "For most plumber companies customers just dials the next result on Google "
    "and a $400 job walks away without automated booking tools requiring manual "
    "contact methods for customers to book appointments quickly I build an AI "
    "system that instantly texts back every missed call answers questions books "
    "straight onto your calendar runs daily with no extra staff needed."
)


# ── Change 1: readability ────────────────────────────────────────────────────

def test_degenerate_qa_draft_is_rejected(prospect):
    violations = check_style(f"missed call at {prospect.name}?", DEGENERATE,
                             prospect, TouchType.EMAIL_1)
    assert violations, "the degenerate QA draft must not pass check_style"
    assert any("word run with no sentence break" in v for v in violations), violations


def test_shipped_template_passes_every_rule(prospect):
    """Regression guard: a new rule that breaks the shipped template fails here."""
    subject, body = render_email_template("initial.j2", prospect)
    assert check_style(subject, body, prospect, TouchType.EMAIL_1) == []


def test_overlong_sentence_is_named(prospect):
    long_sentence = "we " + "really " * 33 + "care about your calls."
    body = (f"Hi Mike,\n\nTampa Bay Plumbing Pros stood out to me.\n\n"
            f"{long_sentence}\n\nDemo here: {get_settings().demo_url}\n\n"
            f"Worth a look? I can show you more.\n\n{_sig()}")
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any("keep every sentence under 32 words" in v for v in violations)


def test_too_few_sentences_flagged(prospect):
    body = (f"Hi Mike,\n\nTampa Bay Plumbing Pros looked great.\n\n"
            f"Demo: {get_settings().demo_url}\n\nWorth a look?\n\n{_sig()}")
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any("at least 4 short ones" in v for v in violations)


def test_missing_terminal_punctuation_flagged(prospect):
    body = (f"Hi Mike,\n\nTampa Bay Plumbing Pros stood out to me today\n\n"
            f"What happens to missed calls? Most walk away. That is real money.\n\n"
            f"Demo: {get_settings().demo_url}\n\nWorth a look?\n\n{_sig()}")
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any("must end with" in v for v in violations)


def test_repeated_phrase_flagged(prospect):
    phrase = "books the job onto your calendar"
    body = (f"Hi Mike,\n\nTampa Bay Plumbing Pros stood out, it {phrase} fast.\n\n"
            f"My system also {phrase} at night. It answers every call.\n\n"
            f"Demo: {get_settings().demo_url}\n\nWorth a look?\n\n{_sig()}")
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any("appears more than once" in v for v in violations)


def test_two_links_in_email1_flagged(prospect):
    body = (f"Hi Mike,\n\nTampa Bay Plumbing Pros stood out to me.\n\n"
            f"See {get_settings().demo_url} and https://other.example/x today.\n\n"
            f"Calls walk away every week. That is money gone.\n\n"
            f"Worth a look?\n\n{_sig()}")
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any("exactly one http link" in v for v in violations)


# ── Change 3: per-prospect subjects ──────────────────────────────────────────

def test_subjects_differ_between_prospects(session):
    a = Prospect(name="Glória Gourmet", city="rio de janeiro", trade="restaurants")
    b = Prospect(name="Bayshore Cooling", city="tampa", trade="hvac")
    session.add_all([a, b])
    session.commit()
    subject_a = template_context(a)["subject_line"]
    subject_b = template_context(b)["subject_line"]
    assert subject_a != subject_b
    # deterministic: the same prospect always gets the same subject
    assert template_context(a)["subject_line"] == subject_a


def test_rendered_template_subject_obeys_the_subject_rules(prospect):
    subject, _ = render_email_template("initial.j2", prospect)
    assert subject  # the SUBJECT: line survived the swap to {{ subject_line }}
    lowered = subject.lower()
    assert prospect.name.lower() in lowered or prospect.city.lower() in lowered
    assert len(subject.split()) <= 8
    assert not subject.rstrip().endswith(".")


def test_subject_rules_reject_bad_subjects(prospect):
    _, body = render_email_template("initial.j2", prospect)
    no_anchor = check_style("quick question", body, prospect, TouchType.EMAIL_1)
    assert any("company name or the city" in v for v in no_anchor)
    too_long = check_style(
        "a very long subject line with far too many words for tampa",
        body, prospect, TouchType.EMAIL_1)
    assert any("8 or fewer" in v for v in too_long)
    full_stop = check_style(f"quick question for {prospect.name}.",
                            body, prospect, TouchType.EMAIL_1)
    assert any("full stop" in v for v in full_stop)


# ── Change 2: placeholder campaign hard block ────────────────────────────────

def test_placeholder_campaign_names_fields_and_blocks_sandbox(session):
    campaign = get_campaign(session)  # env defaults = the example campaign
    fields = campaign.placeholder_fields
    assert "demo_url" in fields
    assert "postal_address" in fields
    assert campaign.is_placeholder  # the dashboard banner keeps working
    assert_campaign_ready("DRY_RUN")  # warns, must not raise
    with pytest.raises(CampaignNotReady) as excinfo:
        assert_campaign_ready("SANDBOX")
    assert "demo_url" in str(excinfo.value)
    with pytest.raises(CampaignNotReady):
        assert_campaign_ready("LIVE")


def test_empty_postal_address_is_a_placeholder_too(session):
    _save_real_campaign(session, postal_address="   ")
    assert "postal_address" in get_campaign(session).placeholder_fields


def _save_real_campaign(session, **overrides):
    data = {
        "company": "Acme Answering", "sender_name": "Jane Doe",
        "sender_role": "founder", "target_niche": "US dental clinics",
        "product_pitch": "a robot receptionist that books jobs",
        "website": "https://acme-answering.io",
        "demo_url": "https://acme-answering.io/demo",
        "calendar_url": "https://cal.acme-answering.io/jane",
        "signature": "Jane Doe, Acme Answering | acme-answering.io",
        "unsubscribe_line": "Reply STOP to opt out.",
        "postal_address": "1 Acme Way, Tampa FL 33601",
        "job_value_by_trade": "dental:900", "default_job_value": 300,
        "missed_calls_per_week": 4,
    }
    data.update(overrides)
    session.add(Connection(kind=ConnectionKind.CAMPAIGN, name="campaign",
                           is_active=True, config_json=data))
    session.commit()


def _real_identity(monkeypatch):
    monkeypatch.setenv("FROM_NAME", "Jane Doe")
    monkeypatch.setenv("FROM_EMAIL", "jane@acme-answering.io")
    get_settings.cache_clear()


def test_real_campaign_is_ready_for_live(session, monkeypatch):
    _save_real_campaign(session)
    _real_identity(monkeypatch)
    assert get_campaign(session).placeholder_fields == []
    assert_campaign_ready("LIVE")  # must not raise


# ── v2.3.1: the From identity joins the gate ─────────────────────────────────

def test_placeholder_from_name_blocks_despite_clean_campaign(session, monkeypatch):
    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Your Name")
    monkeypatch.setenv("FROM_EMAIL", "jane@acme-answering.io")
    get_settings.cache_clear()
    for mode in ("SANDBOX", "LIVE"):
        with pytest.raises(CampaignNotReady) as excinfo:
            assert_campaign_ready(mode)
        assert "from_name" in str(excinfo.value)
        assert "FROM_NAME" in str(excinfo.value)   # points at .env, not /admin
    assert_campaign_ready("DRY_RUN")  # warns, must not raise


def test_empty_from_email_blocks_as_dryrun_localhost(session, monkeypatch):
    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Jane Doe")
    monkeypatch.setenv("FROM_EMAIL", "")
    get_settings.cache_clear()
    with pytest.raises(CampaignNotReady) as excinfo:
        assert_campaign_ready("SANDBOX")
    assert "from_email" in str(excinfo.value)


def test_from_name_falls_back_to_campaign_sender(session, monkeypatch):
    from engine.connections import resolve_email

    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Your Name")
    monkeypatch.setenv("FROM_EMAIL", "jane@acme-answering.io")
    get_settings.cache_clear()
    assert resolve_email(session).from_name == "Jane Doe"


def test_from_name_fallback_without_campaign_keeps_raw(session, monkeypatch):
    """No real campaign to borrow from: the raw value stays, and the gate is
    what blocks it, not a crash or an empty From header."""
    from engine.connections import resolve_email

    monkeypatch.setenv("FROM_NAME", "Your Name")
    get_settings.cache_clear()
    assert resolve_email(session).from_name == "Your Name"


def test_real_from_identity_does_not_raise(session, monkeypatch):
    _save_real_campaign(session)
    _real_identity(monkeypatch)
    assert_campaign_ready("LIVE")  # must not raise
    assert_campaign_ready("SANDBOX")


def _save_email_connection(session, **cfg):
    session.add(Connection(kind=ConnectionKind.EMAIL, name="acct",
                           is_active=True, config_json=cfg))
    session.commit()


def test_placeholder_identity_in_saved_connection_blocks(session, monkeypatch):
    """The connection overrides .env, so its placeholders must gate too."""
    _save_real_campaign(session)
    _real_identity(monkeypatch)  # .env is real; the connection is not
    _save_email_connection(session, from_name="Your Name",
                           from_email="sales@yourdomain.com")
    with pytest.raises(CampaignNotReady) as excinfo:
        assert_campaign_ready("LIVE")
    message = str(excinfo.value)
    assert "from_name" in message
    assert "from_email" in message


def test_real_identity_in_connection_beats_default_env(session, monkeypatch):
    """The reverse: a configured connection must not be blocked by lazy .env."""
    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Your Name")
    monkeypatch.setenv("FROM_EMAIL", "")
    get_settings.cache_clear()
    _save_email_connection(session, from_name="Jane Doe",
                           from_email="jane@acme-answering.io")
    assert_campaign_ready("LIVE")  # must not raise


def test_connection_placeholder_from_name_heals_from_campaign(session, monkeypatch):
    from engine.connections import resolve_email

    _save_real_campaign(session)
    _real_identity(monkeypatch)
    _save_email_connection(session, from_name="Your Name",
                           from_email="jane@acme-answering.io")
    assert resolve_email(session).from_name == "Jane Doe"


def test_blank_from_name_blocks(session, monkeypatch):
    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "")
    monkeypatch.setenv("FROM_EMAIL", "jane@acme-answering.io")
    get_settings.cache_clear()
    with pytest.raises(CampaignNotReady) as excinfo:
        assert_campaign_ready("SANDBOX")
    assert "from_name" in str(excinfo.value)


def test_whitespace_from_email_blocks(session, monkeypatch):
    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Jane Doe")
    monkeypatch.setenv("FROM_EMAIL", "   ")
    get_settings.cache_clear()
    with pytest.raises(CampaignNotReady) as excinfo:
        assert_campaign_ready("LIVE")
    assert "from_email" in str(excinfo.value)


def test_golive_block_message_names_identity(session, monkeypatch):
    from bot.telegram_bot import _campaign_block_message

    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Your Name")
    monkeypatch.setenv("FROM_EMAIL", "jane@acme-answering.io")
    get_settings.cache_clear()
    blocked = _campaign_block_message()
    assert blocked is not None
    assert "from_name" in blocked
    assert "FROM_NAME" in blocked   # points the operator at .env
    _real_identity(monkeypatch)
    assert _campaign_block_message() is None


def test_dry_run_warns_once_naming_identity(session, monkeypatch, caplog):
    import logging

    _save_real_campaign(session)
    monkeypatch.setenv("FROM_NAME", "Your Name")
    monkeypatch.setenv("FROM_EMAIL", "jane@acme-answering.io")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="campaign"):
        assert_campaign_ready("DRY_RUN")
        assert_campaign_ready("DRY_RUN")  # dedupe: same fields, one warning
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "campaign" and "from_name" in r.getMessage()]
    assert len(warnings) == 1


# ── Change 4: owner-name coverage ────────────────────────────────────────────

def _verified_prospect(session, name, owner_name=None):
    p = Prospect(name=name, trade="plumber", city="tampa",
                 email=f"{name.replace(' ', '').lower()}@x.example",
                 email_verification_level=VerificationLevel.MX,
                 owner_name=owner_name, status=ProspectStatus.VERIFIED)
    session.add(p)
    session.commit()
    return p


def test_owner_name_coverage_in_report(session, prospect):
    from engine.reporter import build_report_text, compute_stats

    _verified_prospect(session, "No Name Plumbing")
    stats = compute_stats(session)
    assert stats["owner_named"] == 1        # the fixture prospect has Mike
    assert stats["verified_stage"] == 2
    assert "Owner names: 1 of 2" in build_report_text(session)


def test_require_owner_name_skips_and_leaves_verified(session, monkeypatch):
    from engine.writer import generate_draft

    monkeypatch.setenv("REQUIRE_OWNER_NAME", "true")
    get_settings.cache_clear()
    nameless = _verified_prospect(session, "No Name Plumbing")
    touch = asyncio.run(generate_draft(session, nameless))
    assert touch is None
    assert nameless.status == ProspectStatus.VERIFIED
    assert session.query(Touch).count() == 0


# ── Change 5: the footer stops repeating the signature ───────────────────────

def test_footer_carries_unsubscribe_and_postal_only(session):
    footer = render_footer()
    settings = get_settings()
    assert settings.unsubscribe_line in footer
    assert settings.postal_address in footer          # CAN-SPAM stays
    assert settings.company_website not in footer     # no name | company | site line
    assert settings.from_name not in footer


# ── Change 6: purge-drafts ───────────────────────────────────────────────────

def test_purge_drafts_resets_only_draft_touches(session):
    from engine.pipeline import count_drafts, purge_drafts

    drafted = _verified_prospect(session, "Drafted Co", "Ana Lima")
    drafted.status = ProspectStatus.DRAFTED
    queued = _verified_prospect(session, "Queued Co", "Bo Chen")
    queued.status = ProspectStatus.QUEUED
    contacted = _verified_prospect(session, "Contacted Co", "Cy Diaz")
    contacted.status = ProspectStatus.CONTACTED
    session.add_all([
        Touch(prospect_id=drafted.id, type=TouchType.EMAIL_1,
              status=TouchStatus.DRAFT, subject="s", body="b"),
        Touch(prospect_id=queued.id, type=TouchType.EMAIL_1,
              status=TouchStatus.QUEUED, subject="s", body="b"),
        Touch(prospect_id=contacted.id, type=TouchType.EMAIL_1,
              status=TouchStatus.SENT, subject="s", body="b"),
    ])
    session.commit()
    assert count_drafts(session) == 1

    result = purge_drafts(session)
    assert result["deleted"] == 1
    assert result["reset"] == 1
    session.expire_all()
    assert session.query(Touch).filter(
        Touch.status == TouchStatus.DRAFT).count() == 0
    assert session.query(Touch).filter(
        Touch.status == TouchStatus.QUEUED).count() == 1   # never touched
    assert session.query(Touch).filter(
        Touch.status == TouchStatus.SENT).count() == 1     # never touched
    assert session.get(Prospect, drafted.id).status == ProspectStatus.VERIFIED
    assert session.get(Prospect, queued.id).status == ProspectStatus.QUEUED
    assert session.get(Prospect, contacted.id).status == ProspectStatus.CONTACTED

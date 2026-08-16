"""v2.5 output polish: every quoted bad example from the hand review of the
16 v2.4 drafts is pinned here as a regression test.
"""
import asyncio

from db.models import Prospect, ProspectStatus, Touch, TouchType, VerificationLevel
from engine.config import get_settings
from engine.researcher import geo_mismatch_reason
from engine.targeting import trade_mismatch_reason
from engine.util import utcnow
from engine.writer import (SUBJECT_PATTERNS, check_style, render_email_template,
                           sanitize_detail, template_context)


def _sig() -> str:
    from engine.campaign import get_campaign

    return get_campaign().signature


def _good_body(prospect, greeting="Hi Mike,") -> str:
    return (f"{greeting}\n\nI came across {prospect.name} while researching "
            f"Tampa plumbing companies, and your 4.8 star rating stood out.\n\n"
            f"When a customer calls after hours, what happens to that call? "
            f"For most companies a $500 job walks away.\n\n"
            f"60 second demo: {get_settings().demo_url}\n\nWorth a quick look?\n\n"
            f"{_sig()}")


# ── 1. the greeting is a first name and nothing else ─────────────────────────

def test_pasted_record_greetings_rejected(prospect):
    for greeting in (
        "Hi Ronald Mccrory at Anchor Construction Of Tampa in Tampa, FL,",
        "Hi David Carl Russell at Action Airconditioning Inc., Tampa, FL,",
    ):
        violations = check_style(f"quick question for {prospect.name}",
                                 _good_body(prospect, greeting=greeting),
                                 prospect, TouchType.EMAIL_1)
        assert any("must be exactly" in v for v in violations), greeting


def test_clean_greetings_pass(prospect):
    for greeting in ("Hi Mike,", "Hi there,", "Hi O'neil,"):
        violations = check_style(f"quick question for {prospect.name}",
                                 _good_body(prospect, greeting=greeting),
                                 prospect, TouchType.EMAIL_1)
        assert not any("must be exactly" in v for v in violations), greeting


def test_unblessable_first_name_greets_there(session):
    from engine.writer import greeting_first_name

    allcaps = Prospect(name="X Plumbing", trade="plumber", city="tampa",
                       email="office@x.example", owner_name="RONALD MCCRORY")
    assert greeting_first_name(allcaps) == "there"
    initial_only = Prospect(name="Y Plumbing", trade="plumber", city="tampa",
                            email="office@y.example", owner_name="J A Patterson")
    assert greeting_first_name(initial_only) == "there"


# ── 2. a wrong-city card kills the prospect, not just the detail ─────────────

SIMPSON_CARD = {"human_detail": "20 years serving San Diego",
                "pain_signal": "no chat widget found",
                "bullets": ["Located in Escondido, serving San Diego area"]}


def test_simpson_card_produces_no_draft(session):
    """The actual case: Tampa campaign, San Diego intel card. v2.4 dropped the
    detail and drafted anyway; the model then wrote 'based in Tampa'."""
    from engine.writer import generate_draft

    simpson = Prospect(name="Simpson Mechanical, Inc.", trade="hvac",
                       city="tampa", state="FL",
                       email="office@simpsonmech.example",
                       email_verification_level=VerificationLevel.MX,
                       status=ProspectStatus.VERIFIED,
                       intel_json={"card": dict(SIMPSON_CARD),
                                   "card_fetched_at": utcnow().isoformat()})
    session.add(simpson)
    session.commit()
    touch = asyncio.run(generate_draft(session, simpson))
    assert touch is None
    assert session.query(Touch).count() == 0
    assert simpson.status == ProspectStatus.VERIFIED
    assert "san diego" in (simpson.intel_json or {}).get("geo_mismatch", "")


def test_same_state_service_area_is_not_a_geo_mismatch(session):
    neighbor = Prospect(name="Tampa Cooling", trade="hvac", city="tampa",
                        state="FL",
                        intel_json={"card": {
                            "human_detail": "also serving St Petersburg and Clearwater",
                            "pain_signal": "", "bullets": []}})
    assert geo_mismatch_reason(neighbor) is None


def test_geo_mismatch_counts_in_report(session):
    from engine.reporter import build_report_text, compute_stats

    session.add(Prospect(name="Far Away", trade="hvac", city="tampa", state="FL",
                         intel_json={"geo_mismatch": "intel places ..."}))
    session.commit()
    assert compute_stats(session)["blocked_geo"] == 1
    assert "Never drafted: geo mismatch 1" in build_report_text(session)


# ── 3. trade mismatch blocks the draft ───────────────────────────────────────

def test_anchor_construction_not_drafted(session):
    from engine.writer import generate_draft

    anchor = Prospect(name="Anchor Construction Of Tampa", trade="hvac",
                      city="tampa", state="FL",
                      email="info@anchorconstruction.example",
                      email_verification_level=VerificationLevel.MX,
                      status=ProspectStatus.VERIFIED)
    session.add(anchor)
    session.commit()
    touch = asyncio.run(generate_draft(session, anchor))
    assert touch is None
    assert "construction" in (anchor.intel_json or {}).get("trade_mismatch", "")
    assert session.query(Touch).count() == 0


def test_cogdill_card_not_drafted(session):
    from engine.writer import generate_draft

    cogdill = Prospect(
        name="Cogdill Builders Of Florida Inc", trade="hvac", city="tampa",
        state="FL", email="office@cogdill.example",
        email_verification_level=VerificationLevel.MX,
        status=ProspectStatus.VERIFIED,
        intel_json={"card": {
            "human_detail": "4th generation Floridian with construction experience",
            "pain_signal": "", "bullets": ["Family-owned business with roots in Florida"]},
            "card_fetched_at": utcnow().isoformat()})
    session.add(cogdill)
    session.commit()
    touch = asyncio.run(generate_draft(session, cogdill))
    assert touch is None
    assert (cogdill.intel_json or {}).get("trade_mismatch")


def test_own_trade_name_outranks_clientele_prose():
    """First live run: ten companies named Plumbing were blocked from a
    plumber campaign because their sites mention the remodels and restaurants
    they serve. Identity comes from the name; clientele is not identity."""
    mcnatt = Prospect(name="Mcnatt Plumbing Company, LLC", trade="plumber",
                      city="tampa",
                      intel_json={"about_text": "bathroom remodels, new "
                                                "construction and repipes"})
    assert trade_mismatch_reason(mcnatt) is None
    david_gay = Prospect(name="David Gay Air Conditioning, LLC", trade="hvac",
                         city="tampa",
                         intel_json={"about_text": "restaurant refrigeration "
                                                   "and commercial kitchens"})
    assert trade_mismatch_reason(david_gay) is None
    # A trade-neutral name plus construction prose still blocks.
    neutral = Prospect(name="Integroservices Corp", trade="plumber",
                       city="tampa",
                       intel_json={"about_text": "full service construction "
                                                 "management"})
    assert "construction" in trade_mismatch_reason(neutral)
    # And an off-trade NAME blocks even when it also claims the trade.
    both = Prospect(name="Tampa Plumbing & Construction Services",
                    trade="plumber", city="tampa")
    assert "construction" in trade_mismatch_reason(both)


def test_stale_mismatch_flag_clears_when_rule_no_longer_fires(session):
    from engine.writer import _mismatch_blocks

    healed = Prospect(name="Mcnatt Plumbing Company, LLC", trade="plumber",
                      city="tampa",
                      intel_json={"trade_mismatch": "intel text says 'remodel'",
                                  "about_text": "bathroom remodels and repipes"})
    session.add(healed)
    session.commit()
    assert not _mismatch_blocks(session, healed)
    assert "trade_mismatch" not in (healed.intel_json or {})


def test_trade_check_spares_legitimate_prose_and_own_trade():
    hvac = Prospect(name="Bayshore Cooling And Heating", trade="hvac",
                    city="tampa",
                    intel_json={"about_text": "We are a licensed contractor "
                                              "serving your building's A/C needs."})
    # "contractor" and "building" in PROSE are normal HVAC vocabulary.
    assert trade_mismatch_reason(hvac) is None
    roofer = Prospect(name="Tampa Roofing Pros", trade="roofing", city="tampa")
    assert trade_mismatch_reason(roofer) is None       # their own trade
    builder_in_name = Prospect(name="Automated Building Control Systems Inc",
                               trade="hvac", city="tampa")
    # Deliberately strict: "Building" in the NAME blocks. A skipped prospect
    # costs nothing; a wrong email costs the domain.
    assert "building" in trade_mismatch_reason(builder_in_name)


# ── 4. empty flattery is banned ──────────────────────────────────────────────

FLATTERY_QUOTES = (
    "Your local reputation tells me you have built something worth protecting.",
    "Being in business 20 years tells me you have built something valuable.",
    "You clearly run a solid operation, quality HVAC systems says as much.",
    "Either way, respect for what you've built.",
)


def test_each_quoted_flattery_line_is_rejected(prospect):
    for line in FLATTERY_QUOTES:
        body = _good_body(prospect).replace(
            "Worth a quick look?", f"Worth a quick look?\n\n{line}")
        violations = check_style(f"quick question for {prospect.name}", body,
                                 prospect, TouchType.EMAIL_1)
        assert any("empty flattery" in v for v in violations), line


def test_templates_render_clean_without_any_detail(session):
    """No rating, no reviews, no years: the detail sentence is simply omitted
    and all three templates still pass with zero violations."""
    from engine.writer import INITIAL_TEMPLATES

    for index, name in enumerate(INITIAL_TEMPLATES):
        bare = Prospect(name=f"Bare Plumbing Works {index}", trade="plumber",
                        city="tampa", owner_name="Pat Lee",
                        email=f"office@bare{index}.example",
                        status=ProspectStatus.VERIFIED)
        session.add(bare)
        session.commit()
        assert template_context(bare)["personal_detail"] == ""
        subject, body = render_email_template(name, bare)
        assert check_style(subject, body, bare, TouchType.EMAIL_1) == [], name
        assert "local reputation" not in body.lower()


# ── 5. prose casing ──────────────────────────────────────────────────────────

def test_lowercase_hvac_in_body_rejected(prospect):
    body = _good_body(prospect).replace(
        "your 4.8 star rating stood out",
        "quality hvac systems & supplies factory direct pricing stood out")
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any('write "HVAC"' in v for v in violations)
    ac_body = _good_body(prospect).replace("what happens to that call?",
                                           "who fixes the a/c that night?")
    violations = check_style(f"quick question for {prospect.name}", ac_body,
                             prospect, TouchType.EMAIL_1)
    assert any('write "A/C"' in v for v in violations)


def test_sanitize_detail_fixes_casing():
    assert sanitize_detail("quality hvac systems & supplies") \
        == "quality HVAC systems & supplies"
    assert sanitize_detail("24/7 a/c repair") == "24/7 A/C repair"


def test_detail_casing_fixed_before_templates(session):
    p = Prospect(name="Budget Heating Inc", trade="hvac", city="tampa",
                 intel_json={"years_in_business": None,
                             "card": {"human_detail": "quality hvac systems and supplies",
                                      "pain_signal": "", "bullets": []}})
    session.add(p)
    session.commit()
    context = template_context(p, {"human_detail": "quality hvac systems and supplies"})
    assert "HVAC" in context["personal_detail"]
    assert "hvac" not in context["personal_detail"]


# ── 6. subject collisions ────────────────────────────────────────────────────

def test_generic_pattern_is_gone_and_all_carry_company():
    assert "{city} {trade} question" not in SUBJECT_PATTERNS
    assert all("{company}" in pattern for pattern in SUBJECT_PATTERNS)


def test_generic_subject_rejected_by_distinctive_token_rule(session):
    simpson = Prospect(name="Simpson Mechanical, Inc.", trade="hvac",
                       city="tampa", email="office@simpson.example")
    session.add(simpson)
    session.commit()
    _, body = render_email_template("initial.j2", simpson)
    violations = check_style("Tampa HVAC question", body, simpson,
                             TouchType.EMAIL_1)
    assert any("distinctive word from the company name" in v
               for v in violations)


def test_long_company_subject_falls_back_within_eight_words(session):
    prospects = []
    for index in range(4):   # cover every pattern index
        p = Prospect(name="Automated Building Control Systems Holdings",
                     trade="hvac", city="tampa")
        session.add(p)
        session.commit()
        prospects.append(p)
    for p in prospects:
        subject = template_context(p)["subject_line"]
        assert len(subject.split()) <= 8, subject
        assert "Automated" in subject

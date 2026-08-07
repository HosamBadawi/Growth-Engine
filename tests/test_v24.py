"""v2.4: grounded research, owner-proof verifier, geo/niche gate, owner-name
integrity, writer reliability, template variety.

Every test here pins a failure from a real 23-draft run: prom dresses on an
HVAC company, support@constrafor.com on a plumber, a Rio restaurant drafted a
US missed-call pitch, "Hi Kenneth" to someone called Lindsay, and a draft that
shipped with no signature.
"""
import asyncio

import pytest

from db.models import (ModelRole, Prospect, ProspectStatus, Touch, TouchType,
                       VerificationLevel)
from engine import verifier
from engine.config import get_settings
from engine.researcher import (ResearchProvider, detail_discard_reason,
                               grounding_source, is_repeated_detail,
                               reset_detail_memory)
from engine.verifier import registrable_domain, verify_email_address
from engine.writer import (INITIAL_TEMPLATES, check_style, greeting_first_name,
                           initial_template_for, render_email_template,
                           template_context)


def _mx_ok(monkeypatch):
    monkeypatch.setattr(verifier, "_resolve_mx", lambda d: ["mx1.example.com"])


# ── P0.1 research grounding ──────────────────────────────────────────────────

def test_hallucinated_default_is_ungrounded(prospect):
    source = grounding_source(prospect)  # fixture has no "20 years" anywhere
    reason = detail_discard_reason("20 years serving tampa", source, prospect)
    assert reason and "ungrounded" in reason


def test_sourced_detail_survives(prospect):
    prospect.intel_json = {**(prospect.intel_json or {}),
                           "about_text": "Serving Tampa homeowners for 20 years, "
                                         "family owned and operated."}
    source = grounding_source(prospect)
    assert detail_discard_reason("20 years serving tampa", source, prospect) is None


def test_rating_detail_is_always_provable(prospect):
    # The single best draft of the run used exactly this shape.
    source = grounding_source(prospect)   # rating 4.8, 57 reviews
    assert detail_discard_reason("your 4.8 star rating from 57 customers",
                                 source, prospect) is None


def test_borrowed_city_discarded_even_when_source_contains_it(prospect):
    # Web research can fetch pages about a similarly named San Diego company;
    # overlap would pass, the city check must not.
    source = "20 years serving san diego county with pride " + grounding_source(prospect)
    reason = detail_discard_reason("20 years serving san diego", source, prospect)
    assert reason and "san diego" in reason


def test_off_industry_detail_discarded_even_when_grounded(prospect):
    # Henri's: the crawled site really was a bridal shop, so the detail WAS in
    # the source. Wrong industry beats grounded.
    source = "prom dresses and bridal wear available " + grounding_source(prospect)
    prospect.trade = "hvac"
    reason = detail_discard_reason("prom dresses and bridal wear available",
                                   source, prospect)
    assert reason and "bridal" in reason or "prom" in reason


def test_own_city_is_never_a_wrong_city(prospect):
    prospect.intel_json = {**(prospect.intel_json or {}),
                           "about_text": "proudly serving tampa since 2009"}
    source = grounding_source(prospect)
    assert detail_discard_reason("proudly serving tampa", source, prospect) is None


def test_repeated_detail_memory():
    reset_detail_memory()
    assert not is_repeated_detail("20 years serving tampa", 1)
    assert not is_repeated_detail("20 years serving tampa", 1)  # same prospect: fine
    assert is_repeated_detail("20 years serving tampa", 2)      # second business: no
    assert not is_repeated_detail("4.9 stars across 72 reviews", 2)


class _FakeResearcher(ResearchProvider):
    def __init__(self, card):
        self.card = card

    async def build_card(self, prospect):
        return dict(self.card)


def test_build_intel_card_blanks_hallucination_and_untraceable_owner(
        session, monkeypatch):
    import engine.researcher as researcher_mod

    p = Prospect(name="Gulf Atlantic Mechanical", trade="hvac", city="tampa",
                 status=ProspectStatus.VERIFIED,
                 intel_json={"about_text": "Commercial HVAC service in Tampa."})
    session.add(p)
    session.commit()
    monkeypatch.setattr(researcher_mod, "get_researcher", lambda: _FakeResearcher({
        "owner_name": "Invented Person",
        "human_detail": "affordable protection against legal defense costs",
        "pain_signal": "x", "bullets": []}))
    card = asyncio.run(researcher_mod.build_intel_card(session, p, force=True))
    assert card["human_detail"] == ""        # off-industry AND ungrounded
    assert p.owner_name is None              # not traceable to their own source


def test_cached_card_is_gated_too(session):
    """A pre-v2.4 cached card must not smuggle its hallucination past the gate."""
    from engine.researcher import build_intel_card
    from engine.util import utcnow

    p = Prospect(name="Henri's Heating And Air Conditioning", trade="hvac",
                 city="tampa", status=ProspectStatus.VERIFIED,
                 intel_json={
                     "about_text": "Homecoming 2026 dresses arriving daily!",
                     "card": {"human_detail": "prom dresses and bridal wear available",
                              "pain_signal": "x", "bullets": []},
                     "card_fetched_at": utcnow().isoformat(),  # still fresh
                 })
    session.add(p)
    session.commit()
    card = asyncio.run(build_intel_card(session, p))   # cached path, no LLM
    assert card["human_detail"] == ""


def test_cached_card_noise_detail_is_gated_too(session):
    """Found live: 'best of the bay area sponsored search' sat in a cached card
    predating the ad-noise gate; grounding alone kept it (the words really were
    on the page). Every gate applies on read, including is_clean_detail."""
    from engine.researcher import build_intel_card
    from engine.util import utcnow

    p = Prospect(name="Bay Area Electric", trade="electrician", city="tampa",
                 status=ProspectStatus.VERIFIED,
                 intel_json={
                     "about_text": "best of the bay area sponsored search results",
                     "card": {"human_detail": "best of the bay area sponsored search",
                              "pain_signal": "x", "bullets": []},
                     "card_fetched_at": utcnow().isoformat(),
                 })
    session.add(p)
    session.commit()
    card = asyncio.run(build_intel_card(session, p))
    assert card["human_detail"] == ""


def test_build_intel_card_keeps_traceable_owner(session, monkeypatch):
    import engine.researcher as researcher_mod

    p = Prospect(name="Cogdill Builders", trade="hvac", city="tampa",
                 status=ProspectStatus.VERIFIED,
                 intel_json={"about_text": "Founded by Michael Cogdill in 1998."})
    session.add(p)
    session.commit()
    monkeypatch.setattr(researcher_mod, "get_researcher", lambda: _FakeResearcher({
        "owner_name": "Michael Cogdill", "human_detail": "",
        "pain_signal": "x", "bullets": []}))
    asyncio.run(researcher_mod.build_intel_card(session, p, force=True))
    assert p.owner_name == "Michael Cogdill"


# ── P0.2 verifier ────────────────────────────────────────────────────────────

def test_registrable_domain():
    assert registrable_domain("www.constrafor.com") == "constrafor.com"
    assert registrable_domain("mail.rancho.com.br") == "rancho.com.br"
    assert registrable_domain("shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("gmail.com") == "gmail.com"


def test_malformed_addresses_rejected():
    # The actual queued address, URL-encoding and all.
    result = verify_email_address("%20pat.cox@airpro-systems.com", allow_probe=False)
    assert result.level == VerificationLevel.FAILED
    assert result.reason == "malformed"
    assert verify_email_address("pat cox@x.com", allow_probe=False).reason == "malformed"


def test_role_denylist_rejects_the_run_table():
    for email in ("noreply@williamsmechanical.com", "careers@frenchac.com",
                  "catering@southlandtrade.com", "underwriting@gulfatlantic.com",
                  "billing@x.example", "no-reply@x.example"):
        result = verify_email_address(email, allow_probe=False)
        assert result.level == VerificationLevel.FAILED, email
        assert result.reason == "role_mailbox", email


def test_small_business_role_addresses_stay_allowed(monkeypatch):
    _mx_ok(monkeypatch)
    for local in ("info", "office", "service", "contact", "admin"):
        result = verify_email_address(f"{local}@shop.example", allow_probe=False)
        assert result.level == VerificationLevel.MX, local
        assert result.is_role


def test_domain_mismatch_rejects_the_run_table(monkeypatch):
    _mx_ok(monkeypatch)
    for email, site in (("support@constrafor.com", "https://jagreenplumbing.com"),
                        ("info@bayarea.com", "https://bayareaelectricfl.com"),
                        ("info@davidsmechanicalservices.com",
                         "https://davidgayair.com")):
        result = verify_email_address(email, allow_probe=False, website=site)
        assert result.level == VerificationLevel.FAILED, email
        assert result.reason == "domain_mismatch", email


def test_domain_match_and_free_providers_pass(monkeypatch):
    _mx_ok(monkeypatch)
    ok = verify_email_address("pat@airpro-systems.com", allow_probe=False,
                              website="https://www.airpro-systems.com")
    assert ok.level == VerificationLevel.MX
    sub = verify_email_address("info@mail.acme.example", allow_probe=False,
                               website="https://acme.example")
    assert sub.level == VerificationLevel.MX
    free = verify_email_address("bobtheplumber@gmail.com", allow_probe=False,
                                website="https://bobsplumbing.example")
    assert free.level == VerificationLevel.MX
    no_site = verify_email_address("owner@anything.example", allow_probe=False,
                                   website="")
    assert no_site.level == VerificationLevel.MX


def test_placeholder_local_parts_rejected():
    result = verify_email_address("westchasefamilyplumbingdemo@gmail.com",
                                  allow_probe=False)
    assert result.level == VerificationLevel.FAILED
    assert result.reason == "placeholder"
    assert verify_email_address("test@x.example", allow_probe=False).reason == "placeholder"
    assert verify_email_address("your.name@x.example", allow_probe=False).reason == "placeholder"


def test_placeholder_check_spares_real_words(monkeypatch):
    _mx_ok(monkeypatch)
    # "attestor" contains "test" mid-word; must not be rejected for it.
    result = verify_email_address("attestor@shop.example", allow_probe=False,
                                  website="https://shop.example")
    assert result.level == VerificationLevel.MX


def test_verify_prospect_records_reason(session, prospect):
    prospect.email = "careers@tampabayplumbingpros.example"
    session.commit()
    verifier.verify_prospect(session, prospect)
    assert prospect.email_verification_level == VerificationLevel.FAILED
    assert (prospect.intel_json or {}).get("reject_reason") == "role_mailbox"


def test_reject_breakdown_in_report(session):
    from engine.reporter import build_report_text, compute_stats

    for index, reason in enumerate(("role_mailbox", "role_mailbox",
                                    "domain_mismatch")):
        session.add(Prospect(name=f"r{index}", city="tampa", trade="hvac",
                             email=f"x{index}@x.example",
                             email_verification_level=VerificationLevel.FAILED,
                             intel_json={"reject_reason": reason}))
    session.commit()
    stats = compute_stats(session)
    assert stats["verify_rejects"] == {"role_mailbox": 2, "domain_mismatch": 1}
    assert "Rejected addresses: 3" in build_report_text(session)


# ── P0.3 geo and niche gate ──────────────────────────────────────────────────

def _us_home_services(monkeypatch):
    monkeypatch.setenv("TARGET_COUNTRY", "US")
    monkeypatch.setenv("TARGET_TRADES", "hvac,plumber,electrician")
    get_settings.cache_clear()


def test_targeting_block_reasons(monkeypatch):
    from engine.targeting import targeting_block_reason

    _us_home_services(monkeypatch)
    assert "country BR" in targeting_block_reason("BR", "", "", "hvac")
    assert ".br" in targeting_block_reason(None, "https://rancho.com.br", "", "hvac")
    assert ".uk" in targeting_block_reason(None, "", "x@shop.co.uk", "plumber")
    assert "restaurants" in targeting_block_reason("US", "", "", "restaurants")
    assert targeting_block_reason("US", "https://acme.com", "a@acme.com", "hvac") is None
    assert targeting_block_reason(None, "https://acme.io", "", "plumber") is None

    monkeypatch.setenv("TARGET_COUNTRY", "")
    monkeypatch.setenv("TARGET_TRADES", "")
    get_settings.cache_clear()
    assert targeting_block_reason("BR", "https://x.com.br", "", "restaurants") is None


def test_prospector_filters_foreign_candidates(monkeypatch):
    from engine.prospector import _filter_reason
    from engine.providers.base import RawProspect

    _us_home_services(monkeypatch)
    rio = RawProspect(name="Rancho", city="rio de janeiro", country="BR",
                      category="restaurants", website="https://rancho.com.br")
    assert "country BR" in _filter_reason(rio, require_website=True, trade="restaurants")


def test_rancho_never_reaches_drafted(session, monkeypatch):
    """The actual case: Brazilian restaurant, .com.br email, drafted a US
    missed-call pitch. Must never happen again."""
    from engine.writer import generate_draft

    _us_home_services(monkeypatch)
    rancho = Prospect(name="Rancho", city="rio de janeiro", country="BR",
                      trade="restaurants", email="contato@rancho.com.br",
                      website="https://rancho.com.br",
                      email_verification_level=VerificationLevel.MX,
                      status=ProspectStatus.VERIFIED)
    session.add(rancho)
    session.commit()
    touch = asyncio.run(generate_draft(session, rancho))
    assert touch is None
    assert rancho.status == ProspectStatus.VERIFIED   # left alone, with a reason logged
    assert session.query(Touch).count() == 0


# ── P0.4 owner-name integrity ────────────────────────────────────────────────

def test_owner_collision_blanks_the_second(session):
    """Ken Barrett Air Conditioning and J A Patterson both carried
    'Kenneth James Barrett'; the second must fall back to 'Hi there'."""
    from engine.pipeline import generate_all_drafts

    for name in ("Ken Barrett Air Conditioning Inc", "J A Patterson Htg Inc"):
        session.add(Prospect(
            name=name, trade="hvac", city="tampa",
            email=f"office@{name.split()[0].lower()}.example",
            email_verification_level=VerificationLevel.MX,
            owner_name="Kenneth James Barrett",
            status=ProspectStatus.VERIFIED))
    session.commit()
    asyncio.run(generate_all_drafts())
    session.expire_all()
    first, second = session.query(Prospect).order_by(Prospect.id).all()
    assert first.owner_name == "Kenneth James Barrett"
    assert second.owner_name is None
    assert (second.intel_json or {}).get("suppressed_owner_name") == "Kenneth James Barrett"
    drafts = session.query(Touch).order_by(Touch.id).all()
    assert drafts[0].body.startswith("Hi Kenneth,")
    assert drafts[1].body.startswith("Hi there,")


def test_greeting_falls_back_on_email_owner_mismatch(session):
    """fgolden@ was greeted 'Hi Lindsay'. Personal mailbox, unrelated name:
    greet 'there' instead of asserting a wrong fact."""
    mismatch = Prospect(name="Automated Building Control", trade="hvac",
                        city="tampa", email="fgolden@automatedbuilding.example",
                        owner_name="Lindsay W Byers")
    assert greeting_first_name(mismatch) == "there"
    nickname = Prospect(name="Ken Barrett AC", trade="hvac", city="tampa",
                        email="ken@kenbarrettac.example",
                        owner_name="Kenneth James Barrett")
    assert greeting_first_name(nickname) == "Kenneth"
    role_inbox = Prospect(name="X", trade="hvac", city="tampa",
                          email="office@x.example", owner_name="Lindsay W Byers")
    assert greeting_first_name(role_inbox) == "Lindsay"


# ── P1.5 signature required ──────────────────────────────────────────────────

def test_draft_34_shape_missing_signature_rejected(prospect):
    body = (
        "Hi Enrique,\n\n"
        "I came across Tampa Bay Plumbing Pros while researching Tampa HVAC "
        "companies.\n\n"
        "When a customer calls after hours, what happens to that call? Most "
        "owners never find out.\n\n"
        "Without a chat widget, those calls might miss out on a $500 job.\n\n"
        f"Demo link: {get_settings().demo_url}\n\n"
        "Should I send you the demo?"
    )
    violations = check_style(f"quick question for {prospect.name}", body,
                             prospect, TouchType.EMAIL_1)
    assert any("must end with the signature" in v for v in violations)


# ── P1.6 subject never comes from the model ──────────────────────────────────

def test_generate_draft_asks_only_for_body(session, prospect, monkeypatch):
    import engine.writer as writer_mod

    captured = {}
    valid_body = render_email_template(
        initial_template_for(prospect), prospect)[1]

    async def fake_chat(role, system, user, required_keys, max_attempts=3,
                        temperature=0.4):
        captured.update(role=role, required_keys=required_keys,
                        temperature=temperature, system=system)
        return {"body": valid_body}

    monkeypatch.setattr(writer_mod, "llm_chat_json", fake_chat)
    touch = asyncio.run(writer_mod.generate_draft(session, prospect))
    assert captured["required_keys"] == ["body"]
    assert captured["temperature"] == 0.35
    assert '"subject"' not in captured["system"]
    expected_subject = template_context(prospect)["subject_line"]
    assert touch.subject == expected_subject
    assert (touch.meta_json or {}).get("source") == "llm"


# ── P1.7 retry discipline ────────────────────────────────────────────────────

class _Recorder:
    """FakeProvider that records every prompt it is sent."""

    def __init__(self, replies):
        from engine.llm.base import LLMProvider

        outer = self

        class _P(LLMProvider):
            provider_type = "fake"

            async def _chat_raw(self, model, system, user, json_mode, temperature):
                outer.prompts.append(user)
                return replies.pop(0)

            async def list_models(self):
                return []

        self.prompts: list[str] = []
        self.provider = _P(base_url="http://x", label="rec")


def test_json_retry_prompt_shrinks_and_logs_raw(caplog):
    import logging

    recorder = _Recorder(["not json", "still not json", '{"body": "ok"}'])
    long_user = "task " * 800  # ~4000 chars
    with caplog.at_level(logging.WARNING, logger="llm"):
        data = asyncio.run(recorder.provider.chat_json(
            "m", "sys", long_user, required_keys=["body"]))
    assert data == {"body": "ok"}
    first, second, third = recorder.prompts
    assert len(second) < len(first)          # shorter, not longer
    assert len(third) <= len(second) + 50    # and it stops growing
    assert any("raw output" in r.getMessage() for r in caplog.records)


# ── P1.8 per-role fallback chain ─────────────────────────────────────────────

async def _chain_test(session, monkeypatch):
    import engine.llm as llm_mod
    from engine.llm import llm_chat_text
    from engine.llm.base import LLMError, LLMProvider

    class ModelPicky(LLMProvider):
        provider_type = "ollama"

        def __init__(self, **kw):
            super().__init__(**kw)
            self.calls = []

        async def _chat_raw(self, model, system, user, json_mode, temperature):
            self.calls.append(model)
            if model == "qwen-custom":
                raise LLMError("model not found")
            return "recovered"

        async def list_models(self):
            return []

    picky = ModelPicky(base_url="http://x", label="local ollama")
    monkeypatch.setattr(llm_mod, "_local_ollama", lambda: picky)
    session.add(ModelRole(role="writer", llm_connection_id=None,
                          model_name="qwen-custom"))
    session.commit()
    result = await llm_chat_text("writer", "sys", "user")
    return picky, result


def test_assigned_local_model_falls_back_to_env_default(session, monkeypatch):
    from db.models import Event

    picky, result = asyncio.run(_chain_test(session, monkeypatch))
    assert result == "recovered"
    assert picky.calls == ["qwen-custom", get_settings().writer_model]
    events = session.query(Event).filter(Event.module == "llm").all()
    assert any(e.message.startswith("LLM fallback:") for e in events)


def test_single_candidate_role_still_raises(session, monkeypatch):
    import engine.llm as llm_mod
    from engine.llm import llm_chat_text
    from engine.llm.base import LLMError, LLMProvider

    class AlwaysDown(LLMProvider):
        provider_type = "ollama"

        async def _chat_raw(self, model, system, user, json_mode, temperature):
            raise LLMError("down")

        async def list_models(self):
            return []

    monkeypatch.setattr(llm_mod, "_local_ollama",
                        lambda: AlwaysDown(base_url="http://x", label="local ollama"))
    with pytest.raises(LLMError):
        asyncio.run(llm_chat_text("writer", "sys", "user"))


# ── P1.9 template variety ────────────────────────────────────────────────────

def test_three_initial_variants_all_pass_check_style(session):
    for index, name in enumerate(INITIAL_TEMPLATES):
        p = Prospect(name=f"Variant Test Plumbing {index}", trade="plumber",
                     city="tampa", email=f"owner{index}@variant{index}.example",
                     owner_name="Pat Lee", rating=4.9, review_count=72,
                     status=ProspectStatus.VERIFIED)
        session.add(p)
        session.commit()
        subject, body = render_email_template(name, p)
        assert check_style(subject, body, p, TouchType.EMAIL_1) == [], name


def test_initial_template_selected_by_id(session):
    prospects = []
    for index in range(3):
        p = Prospect(name=f"T{index}", trade="hvac", city="tampa")
        session.add(p)
        session.commit()
        prospects.append(p)
    names = {initial_template_for(p) for p in prospects}
    assert names == set(INITIAL_TEMPLATES)


# ── P1.10 display casing ─────────────────────────────────────────────────────

def test_trade_display_and_company_cleanup(session):
    p = Prospect(name="Southland Restaurant Services,llc", trade="hvac",
                 city="tampa")
    session.add(p)
    session.commit()
    context = template_context(p)
    assert context["trade"] == "HVAC"
    assert context["company"] == "Southland Restaurant Services, llc"
    assert "Southland Restaurant Services, LLC" in context["subject_line"]

    plumber = Prospect(name="Bob's Plumbing", trade="plumber", city="tampa")
    session.add(plumber)
    session.commit()
    assert template_context(plumber)["trade"] == "plumbing"

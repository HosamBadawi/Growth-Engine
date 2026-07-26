"""Phase 4 (no-website segment, second offer) and Phase 5 (backfill)."""
import pytest
from fastapi.testclient import TestClient

from db.models import (Connection, ConnectionKind, Prospect, ProspectStatus,
                       VerificationLevel)
from engine.stages import (SEGMENT_EMAILABLE, SEGMENT_MANUAL,
                           SEGMENT_NO_WEBSITE, in_segment, segment_counts)


@pytest.fixture
def client(session):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed(client):
    from engine.config import get_settings

    response = client.post("/login", data={"password": get_settings().dashboard_password},
                           follow_redirects=False)
    assert response.status_code == 303
    return client


def _p(session, **kw):
    defaults = dict(name="X Co", city="rio", trade="restaurants",
                    status=ProspectStatus.VERIFIED)
    defaults.update(kw)
    prospect = Prospect(**defaults)
    session.add(prospect)
    session.commit()
    return prospect


# ── Phase 4: segments are derived, never stored ─────────────────────────────

def test_no_website_segment(session):
    without = _p(session, name="No Site Co")
    with_site = _p(session, name="Has Site Co", website="https://x.com.br")
    assert in_segment(without, SEGMENT_NO_WEBSITE)
    assert not in_segment(with_site, SEGMENT_NO_WEBSITE)


def test_emailable_and_manual_are_disjoint(session):
    verified = _p(session, name="Verified Co", email="a@b.example",
                  email_verification_level=VerificationLevel.MX)
    unverified = _p(session, name="Unverified Co", email="c@d.example",
                    email_verification_level=VerificationLevel.NONE,
                    phone="+5521999998888")
    social_only = _p(session, name="Social Co",
                     social_links={"instagram": "https://www.instagram.com/x"})
    nothing = _p(session, name="Nothing Co")

    assert in_segment(verified, SEGMENT_EMAILABLE)
    assert not in_segment(verified, SEGMENT_MANUAL)

    # an unverified address is NOT emailable; the engine will not use it
    assert not in_segment(unverified, SEGMENT_EMAILABLE)
    assert in_segment(unverified, SEGMENT_MANUAL)

    assert in_segment(social_only, SEGMENT_MANUAL)
    assert not in_segment(nothing, SEGMENT_MANUAL)   # no channel at all


def test_segment_counts_and_page_filter(authed, session):
    _p(session, name="No Site Co")
    _p(session, name="Has Site Co", website="https://x.com.br",
       email="a@b.example", email_verification_level=VerificationLevel.MX)

    counts = segment_counts(session, "prospects")
    assert counts[SEGMENT_NO_WEBSITE] == 1
    assert counts[SEGMENT_EMAILABLE] == 1

    page = authed.get("/pipeline/prospects?segment=no_website").text
    assert "No Site Co" in page
    assert "Has Site Co" not in page
    assert "never auto-emailed" in page      # the honest label is on the page


def test_segment_export_respects_filter(authed, session):
    _p(session, name="No Site Co")
    _p(session, name="Has Site Co", website="https://x.com.br")
    text = authed.get(
        "/pipeline/export/prospects.csv?segment=no_website").content.decode("utf-8-sig")
    assert "No Site Co" in text and "Has Site Co" not in text


# ── Phase 4: a second offer for the no-website segment ──────────────────────

def _campaign(session, name, segment, **over):
    cfg = {"company": f"{name} Co", "product_pitch": f"{name} pitch",
           "sender_name": "Jane Doe", "signature": f"Jane Doe, {name} Co",
           "demo_url": f"https://{name.lower()}.example/demo",
           "calendar_url": f"https://{name.lower()}.example/call",
           "segment": segment}
    cfg.update(over)
    conn = Connection(kind=ConnectionKind.CAMPAIGN, name=name,
                      is_active=(segment == ""), config_json=cfg)
    session.add(conn)
    session.commit()
    return conn


def test_second_campaign_serves_the_no_website_segment(session):
    from engine.writer import campaign_for

    _campaign(session, "Missed", "")            # default offer
    _campaign(session, "WebBuild", "no_website")  # second offer

    has_site = _p(session, name="Has Site Co", website="https://x.com.br")
    no_site = _p(session, name="No Site Co")

    assert campaign_for(has_site).company == "Missed Co"
    assert campaign_for(no_site).company == "WebBuild Co"


def test_single_campaign_setups_are_unchanged(session):
    """No segment campaign configured -> everyone gets the default offer."""
    from engine.writer import campaign_for

    _campaign(session, "Only", "")
    assert campaign_for(_p(session, name="No Site Co")).company == "Only Co"


def test_offer_reaches_the_rendered_email(session):
    from engine.writer import render_email_template

    _campaign(session, "Missed", "")
    _campaign(session, "WebBuild", "no_website")
    no_site = _p(session, name="No Site Co", owner_name="Ana Silva")
    _subject, body = render_email_template("initial.j2", no_site)
    assert "WebBuild pitch" in body
    assert "Missed pitch" not in body


# ── Phase 5: backfill ───────────────────────────────────────────────────────

def test_backfill_candidates_match_each_filter(session):
    from engine.backfill import candidates

    _p(session, name="No Site Co")
    _p(session, name="Full Co", website="https://x.com.br", email="a@b.example",
       phone="+1", contact_form_url="https://x.com.br/c")
    _p(session, name="New Co", status=ProspectStatus.NEW,
       website="https://y.com.br")

    assert [p.name for p in candidates(session, "no_website")] == ["No Site Co"]
    assert "No Site Co" in [p.name for p in candidates(session, "no_email")]
    assert [p.name for p in candidates(session, "never_enriched")] == ["New Co"]
    assert "Full Co" not in [p.name for p in candidates(session, "no_contact")]


def test_backfill_skips_suppressed(session):
    from engine.backfill import candidates

    _p(session, name="Suppressed Co", status=ProspectStatus.SUPPRESSED)
    assert candidates(session, "no_website") == []


def test_backfill_runs_and_reports(session, monkeypatch):
    import engine.backfill as backfill

    target = _p(session, name="No Site Co")

    def fake_enrich(sess, prospect, **kw):
        prospect.website = "https://recovered.com.br"
        prospect.status = ProspectStatus.ENRICHED
        sess.commit()

    monkeypatch.setattr("engine.enricher.enrich_prospect", fake_enrich)
    lines: list[str] = []
    summary = backfill.run_backfill(session, "no_website", 10, lines.append)

    assert summary["processed"] == 1
    assert summary["websites_found"] == 1
    assert any("No Site Co" in line for line in lines)   # progress reported
    session.refresh(target)
    assert target.website == "https://recovered.com.br"


def test_backfill_revives_unreachable_when_evidence_appears(session, monkeypatch):
    import engine.backfill as backfill

    dead = _p(session, name="Dead Co", status=ProspectStatus.UNREACHABLE,
              attempt_count=3)

    def fake_enrich(sess, prospect, **kw):
        prospect.website = "https://alive.com.br"
        sess.commit()

    monkeypatch.setattr("engine.enricher.enrich_prospect", fake_enrich)
    backfill.run_backfill(session, "unreachable", 10)

    session.refresh(dead)
    assert dead.status == ProspectStatus.ENRICHED   # new evidence beats an old verdict
    assert dead.attempt_count == 0


def test_backfill_one_failure_does_not_stop_the_batch(session, monkeypatch):
    import engine.backfill as backfill

    _p(session, name="Bad Co")
    _p(session, name="Good Co")

    def flaky(sess, prospect, **kw):
        if prospect.name == "Bad Co":
            raise RuntimeError("boom")
        prospect.website = "https://ok.com.br"
        sess.commit()

    monkeypatch.setattr("engine.enricher.enrich_prospect", flaky)
    summary = backfill.run_backfill(session, "no_website", 10)
    assert summary["failed"] == 1 and summary["processed"] == 1


def test_backfill_clears_stale_directory_urls(session):
    """v2.1 stored directory URLs as websites. A directory URL is worse than
    none: the crawler mines it and can hand the sender a stranger's address."""
    from engine.discovery import DiscoveryResult
    from engine.enricher import _apply_discovery

    prospect = _p(session, name="J.A. Green Plumbing",
                  website="https://www.allbiz.com/business/j-a-green-plumbing")
    _apply_discovery(prospect, DiscoveryResult())      # ladder found nothing real
    session.commit()

    assert prospect.website is None
    assert "allbiz" in prospect.intel_json["demoted_listing"]


def test_real_website_is_not_cleared(session):
    from engine.discovery import DiscoveryResult
    from engine.enricher import _apply_discovery

    prospect = _p(session, name="Real Co", website="https://realco.com.br")
    _apply_discovery(prospect, DiscoveryResult())
    assert prospect.website == "https://realco.com.br"


def test_backfill_rejects_unknown_filter(session):
    import engine.backfill as backfill

    with pytest.raises(ValueError, match="Unknown backfill filter"):
        backfill.run_backfill(session, "nonsense", 5)


def test_enrich_action_requires_auth(client):
    response = client.post("/actions/enrich", data={}, follow_redirects=False)
    assert response.headers["location"] == "/login"


def test_enrich_action_validates_filter(authed):
    response = authed.post("/actions/enrich",
                           data={"filter_key": "nope", "count": "5"},
                           follow_redirects=False)
    assert "Unknown+filter" in response.headers["location"] \
        or "Unknown filter" in response.headers["location"].replace("%20", " ")


def test_enrich_action_starts_a_job(authed):
    response = authed.post("/actions/enrich",
                           data={"filter_key": "no_website", "count": "5"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "job=" in response.headers["location"]

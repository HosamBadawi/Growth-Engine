"""Phase 3, getting the data out: view exports, run exports, rejects visible."""
import pytest
from fastapi.testclient import TestClient

from db.models import (FindRun, Prospect, ProspectStatus, RejectedCandidate,
                       VerificationLevel)


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


@pytest.fixture
def seeded(session):
    run = FindRun(query="restaurants rio de janeiro", provider="osm", target=10,
                  summary_json={"kept": 1, "skipped": {"no website": 1},
                                "prospect_ids": [], "skipped_known": 0})
    session.add(run)
    session.commit()
    kept = Prospect(name="Glória Gourmet", city="rio de janeiro", country="BR",
                    trade="restaurants", status=ProspectStatus.VERIFIED,
                    website="https://gloriagourmet.com.br",
                    email="contato@gloriagourmet.com.br",
                    email_verification_level=VerificationLevel.MX,
                    social_links={"instagram": "https://www.instagram.com/gloria"},
                    provenance={"website": {"rung": "B", "source": "domain guess",
                                            "confidence": 0.75}})
    session.add(kept)
    session.commit()
    run.summary_json = {**run.summary_json, "prospect_ids": [kept.id]}
    session.add(RejectedCandidate(
        run_id=run.id, name="مطعم كازينو الإيطالي", city="giza", country="EG",
        reason="no website",
        raw_json={"phone": "+20100000000", "source": "osm",
                  "dedupe_key": "مطعم|giza"}))
    session.commit()
    return run


def test_stage_export_is_utf8_sig_and_renders_names(authed, seeded):
    """Gate 10: Portuguese and Arabic names must survive the round trip."""
    response = authed.get("/pipeline/export/prospects.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    raw = response.content
    assert raw.startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM: Excel shows mojibake"
    text = raw.decode("utf-8-sig")
    assert "Glória Gourmet" in text          # accents intact, not 'GlÃ³ria'
    assert "Ã" not in text
    header = text.splitlines()[0]
    for column in ("social_links", "provenance", "channel", "stage", "country"):
        assert column in header, column


def test_export_includes_provenance_and_channel(authed, seeded):
    text = authed.get("/pipeline/export/prospects.csv").content.decode("utf-8-sig")
    assert "domain guess" in text            # provenance travels with the row
    assert "email" in text                   # channel column


def test_run_export_includes_rejected_rows(authed, seeded):
    """The rows v2.1 discarded behind the number '10' are now exportable."""
    response = authed.get(f"/pipeline/runs/{seeded.id}.csv")
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    assert "kept" in text and "rejected" in text
    assert "Glória Gourmet" in text
    assert "مطعم كازينو الإيطالي" in text     # Arabic reject survives too
    assert "no website" in text              # with its reason


def test_runs_page_lists_runs_and_rejects(authed, seeded):
    page = authed.get("/pipeline/runs").text
    assert "restaurants rio de janeiro" in page
    assert "no website" in page
    assert "Retry discovery" in page


def test_runs_page_requires_auth(client):
    response = client.get("/pipeline/runs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_retry_reject_recovers_when_a_site_is_found(authed, session, seeded,
                                                    monkeypatch):
    import engine.prospector as prospector

    monkeypatch.setattr(prospector, "_try_website",
                        lambda raw: "https://recovered.com.eg")
    reject = session.query(RejectedCandidate).first()
    response = authed.post(f"/pipeline/rejects/{reject.id}/retry",
                           follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    recovered = session.query(Prospect).filter(
        Prospect.website == "https://recovered.com.eg").one()
    assert recovered.country == "EG"
    assert session.get(RejectedCandidate, reject.id).retried is True


def test_retry_reject_leaves_it_rejected_when_nothing_found(authed, session,
                                                            seeded, monkeypatch):
    import engine.prospector as prospector

    monkeypatch.setattr(prospector, "_try_website", lambda raw: "")
    reject = session.query(RejectedCandidate).first()
    before = session.query(Prospect).count()
    authed.post(f"/pipeline/rejects/{reject.id}/retry", follow_redirects=False)
    session.expire_all()
    assert session.query(Prospect).count() == before   # no junk prospect created


def test_search_export_respects_the_query(authed, seeded):
    text = authed.get("/pipeline/export/search.csv?q=gloria").content.decode("utf-8-sig")
    assert "Glória Gourmet" in text
    text_miss = authed.get(
        "/pipeline/export/search.csv?q=zzzznomatch").content.decode("utf-8-sig")
    assert "Glória Gourmet" not in text_miss

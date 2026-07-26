"""Pipeline UI routes: auth, stage rendering, money-page redirect, legacy /leads."""
import pytest
from fastapi.testclient import TestClient

from db.models import Prospect, ProspectStatus, Reply, ReplyClass, Touch, TouchStatus


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


def test_pipeline_requires_auth(client):
    response = client.get("/pipeline/prospects", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_stage_pages_render_and_partition(authed, session):
    session.add(Prospect(name="Fresh Co", status=ProspectStatus.VERIFIED,
                         city="tampa", trade="hvac"))
    drafted = Prospect(name="Drafted Co", status=ProspectStatus.DRAFTED,
                       city="tampa", trade="hvac")
    session.add(drafted)
    session.commit()
    session.add(Touch(prospect_id=drafted.id, type="EMAIL_1", subject="s",
                      body="b", status=TouchStatus.DRAFT))
    session.commit()

    prospects_page = authed.get("/pipeline/prospects").text
    drafts_page = authed.get("/pipeline/drafts").text
    assert "Fresh Co" in prospects_page and "Fresh Co" not in drafts_page
    assert "Drafted Co" in drafts_page and "Drafted Co" not in prospects_page


def test_pipeline_home_prefers_replies_when_nonempty(authed, session):
    response = authed.get("/pipeline", follow_redirects=False)
    assert response.headers["location"] == "/pipeline/prospects"

    p = Prospect(name="Replied Co", status=ProspectStatus.REPLIED,
                 city="tampa", trade="hvac")
    session.add(p)
    session.commit()
    session.add(Reply(prospect_id=p.id, classification=ReplyClass.QUESTION,
                      raw_text="interested, call me"))
    session.commit()

    response = authed.get("/pipeline", follow_redirects=False)
    assert response.headers["location"] == "/pipeline/replies"
    assert "Replied Co" in authed.get("/pipeline/replies").text


def test_legacy_leads_redirects(authed):
    response = authed.get("/leads", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/pipeline"
    response = authed.get("/leads?q=acme", follow_redirects=False)
    assert response.headers["location"] == "/pipeline/search?q=acme"


def test_unknown_stage_redirects_home(authed):
    response = authed.get("/pipeline/nonsense", follow_redirects=False)
    assert response.status_code == 303


def test_global_search_page(authed, session):
    session.add(Prospect(name="Searchable Plumbing", status=ProspectStatus.NEW,
                         city="tampa", trade="plumber"))
    session.commit()
    page = authed.get("/pipeline/search?q=searchable").text
    assert "Searchable Plumbing" in page

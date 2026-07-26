"""Phase 4 web parity actions: auth, job runner, pause/resume, LIVE stays dual-key."""
import pytest
from fastapi.testclient import TestClient

from db.models import Prospect, ProspectStatus, Touch, TouchStatus


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


ACTION_POSTS = ["/actions/find", "/actions/draft", "/actions/queue",
                "/actions/pause", "/actions/resume", "/actions/golive-request"]


def test_actions_require_auth(client):
    response = client.get("/actions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    for path in ACTION_POSTS:
        response = client.post(path, data={}, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


def test_actions_page_renders(authed):
    page = authed.get("/actions").text
    assert "Find prospects" in page
    assert "DRY_RUN" in page


def test_find_validates_input(authed):
    response = authed.post("/actions/find",
                           data={"niche": "hvac", "city": "tampa", "count": "abc"},
                           follow_redirects=False)
    assert "number" in response.headers["location"]
    response = authed.post("/actions/find",
                           data={"niche": "   ", "city": "tampa", "count": "5"},
                           follow_redirects=False)
    assert "required" in response.headers["location"]


def test_pause_and_resume_roundtrip(authed, session):
    from engine.state import is_paused

    authed.post("/actions/pause", follow_redirects=False)
    assert is_paused(session) is True

    p = Prospect(name="Paused Co", status=ProspectStatus.QUEUED, city="tampa")
    session.add(p)
    session.commit()
    touch = Touch(prospect_id=p.id, type="EMAIL_1", subject="s", body="b",
                  status=TouchStatus.PAUSED)
    session.add(touch)
    session.commit()

    authed.post("/actions/resume", follow_redirects=False)
    assert is_paused(session) is False
    session.refresh(touch)
    assert touch.status == TouchStatus.QUEUED  # paused touches re-queued


def test_queue_approved_from_web(authed, session):
    from db.models import VerificationLevel

    p = Prospect(name="Approved Co", status=ProspectStatus.VERIFIED, city="tampa",
                 email="a@b.example",
                 email_verification_level=VerificationLevel.MX)
    session.add(p)
    session.commit()
    touch = Touch(prospect_id=p.id, type="EMAIL_1", subject="s", body="b",
                  status=TouchStatus.APPROVED)
    session.add(touch)
    session.commit()

    authed.post("/actions/queue", follow_redirects=False)
    session.refresh(touch)
    assert touch.status == TouchStatus.QUEUED


def test_unverified_prospect_is_refused_at_queue_time(authed, session):
    """Safety property, not a UI hint: an unverified address never enters a
    sequence, even if a draft for it was approved."""
    from db.models import VerificationLevel

    p = Prospect(name="No Website Co", status=ProspectStatus.DRAFTED, city="rio",
                 email="maybe@unverified.example",
                 email_verification_level=VerificationLevel.NONE,
                 social_links={"instagram": "https://www.instagram.com/x"})
    session.add(p)
    session.commit()
    touch = Touch(prospect_id=p.id, type="EMAIL_1", subject="s", body="b",
                  status=TouchStatus.APPROVED)
    session.add(touch)
    session.commit()

    authed.post("/actions/queue", follow_redirects=False)
    session.refresh(touch)
    assert touch.status == TouchStatus.CANCELLED
    assert touch.meta_json["cancel_reason"] == "email not verified"


def test_golive_request_refuses_unless_env_is_live(authed, session):
    """Mode is .env-only: the web UI can never flip it."""
    from engine.state import is_live_confirmed

    response = authed.post("/actions/golive-request", follow_redirects=False)
    assert "ENGINE_MODE%20is%20not%20LIVE" in response.headers["location"] \
        or "not LIVE" in response.headers["location"].replace("%20", " ")
    assert is_live_confirmed(session) is False  # nothing confirmed by the web call


def test_golive_request_in_live_mode_needs_telegram(authed, session, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "LIVE")
    from engine.config import get_settings
    get_settings.cache_clear()
    from engine.state import is_live_confirmed

    response = authed.post("/actions/golive-request", follow_redirects=False)
    location = response.headers["location"].replace("%20", " ")
    # no bot configured in tests -> must say so, and must NOT self-confirm
    assert "Telegram" in location
    assert is_live_confirmed(session) is False


def test_job_endpoint_reports_unknown_job(authed):
    assert authed.get("/actions/job/999999").json() == {"found": False}

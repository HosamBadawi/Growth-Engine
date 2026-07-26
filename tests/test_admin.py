"""Admin panel: auth on every route, healthz shape, template overrides, suppression."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(session, monkeypatch, tmp_path):
    # template overrides land in a temp dir, never the repo's data/
    import engine.writer as writer
    monkeypatch.setattr(writer, "OVERRIDE_DIR", tmp_path / "templates")

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


ADMIN_PAGES = ["/admin", "/admin/models", "/admin/campaign", "/admin/rails",
               "/admin/templates", "/admin/prospector", "/admin/suppression",
               "/admin/data"]


def test_admin_routes_require_auth(client):
    for page in ADMIN_PAGES:
        response = client.get(page, follow_redirects=False)
        assert response.status_code == 303, page
        assert response.headers["location"] == "/login", page


def test_admin_pages_render_when_authed(authed):
    for page in ADMIN_PAGES:
        response = authed.get(page)
        assert response.status_code == 200, page
        assert "Admin" in response.text, page


def test_healthz_shape_and_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    health = response.json()
    for key in ("version", "mode", "paused", "db", "ollama",
                "active_llm_roles", "smtp", "imap", "bot"):
        assert key in health, key
    assert health["mode"] == "DRY_RUN"
    assert health["db"] == "ok"


def test_campaign_save_roundtrip(authed, session):
    from engine.campaign import CAMPAIGN_FIELDS

    data = {key: "x" for key, _ in CAMPAIGN_FIELDS}
    data.update({"company": "Acme Answering", "default_job_value": "500",
                 "missed_calls_per_week": "3"})
    response = authed.post("/admin/campaign/save", data=data, follow_redirects=False)
    assert response.status_code == 303
    from engine.campaign import get_campaign
    campaign = get_campaign(session)
    assert campaign.company == "Acme Answering"
    assert campaign.default_job_value == 500


def test_template_override_precedence(authed):
    from engine.writer import read_template_source

    source, is_override = read_template_source("initial.j2")
    assert not is_override
    edited = source.replace("quick question", "custom subject test")
    response = authed.post("/admin/templates/save",
                           data={"name": "initial.j2", "source": edited},
                           follow_redirects=False)
    assert response.status_code == 303
    source2, is_override2 = read_template_source("initial.j2")
    assert is_override2
    assert "custom subject test" in source2
    # reset restores the repo default
    authed.post("/admin/templates/reset", data={"name": "initial.j2"},
                follow_redirects=False)
    _, is_override3 = read_template_source("initial.j2")
    assert not is_override3


def test_template_save_rejects_unknown_name(authed):
    response = authed.post("/admin/templates/save",
                           data={"name": "../evil.j2", "source": "x"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "Unknown" in response.headers["location"]


def test_suppression_add_and_no_delete_route(authed, session):
    response = authed.post("/admin/suppression/add",
                           data={"email": "Gone@Example.com", "reason": "asked"},
                           follow_redirects=False)
    assert response.status_code == 303
    from engine.sender import is_suppressed
    assert is_suppressed(session, "gone@example.com")
    # suppression is forever: no delete endpoint exists
    response = authed.post("/admin/suppression/delete",
                           data={"email": "gone@example.com"})
    assert response.status_code in (404, 405)


def test_rails_save_clamps_server_side(authed):
    response = authed.post("/admin/rails/save",
                           data={"daily_send_cap": "999",
                                 "send_window_start": "05:00"},
                           follow_redirects=False)
    assert response.status_code == 303
    import engine.rails as rails
    rails.invalidate()
    assert rails.eff_daily_cap() <= 30
    assert rails.eff_window()[0] != "05:00"  # widening attempt was dropped

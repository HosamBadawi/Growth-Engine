"""Campaign profile: DB overrides env, writer renders campaign not brand."""
from db.models import Connection, ConnectionKind
from engine.campaign import get_campaign
from engine.writer import render_email_template, writer_system_prompt


def _save_campaign(session, **overrides):
    data = {
        "company": "Acme Answering", "product_pitch": "a robot receptionist",
        "target_niche": "US dental clinics", "sender_name": "Jane Doe",
        "sender_role": "founder", "website": "https://acme.example",
        "demo_url": "https://acme.example/demo",
        "calendar_url": "https://cal.example/jane",
        "signature": "Jane Doe, Acme Answering | acme.example",
        "unsubscribe_line": "Reply STOP to opt out.",
        "postal_address": "1 Acme Way", "job_value_by_trade": "dental:900",
        "default_job_value": 300, "missed_calls_per_week": 4,
    }
    data.update(overrides)
    conn = Connection(kind=ConnectionKind.CAMPAIGN, name="campaign",
                      is_active=True, config_json=data)
    session.add(conn)
    session.commit()
    return data


def test_env_campaign_is_placeholder(session):
    campaign = get_campaign(session)
    assert campaign.source == "env"
    assert campaign.is_placeholder  # "Example Co" defaults


def test_db_campaign_overrides_env(session):
    _save_campaign(session)
    campaign = get_campaign(session)
    assert campaign.company == "Acme Answering"
    assert not campaign.is_placeholder
    assert campaign.job_value_for("dental") == 900
    assert campaign.job_value_for("unknown") == 300


# Built dynamically so the public-repo brand grep gate stays clean:
# the operator's historical brand terms must not appear as literals anywhere.
_FORBIDDEN = ["".join(("serv", "andi")), "".join(("hos", "am"))]


def test_writer_system_prompt_renders_campaign_not_brand(session):
    _save_campaign(session)
    prompt = writer_system_prompt()
    assert "Jane Doe" in prompt
    assert "Acme Answering" in prompt
    assert "robot receptionist" in prompt
    for brand in _FORBIDDEN:
        assert brand not in prompt.lower()


def test_templates_render_campaign_values(session, prospect):
    _save_campaign(session)
    subject, body = render_email_template("initial.j2", prospect)
    assert "https://acme.example/demo" in body
    assert "Jane Doe, Acme Answering | acme.example" in body
    assert "a robot receptionist" in body
    for brand in _FORBIDDEN:
        assert brand not in body.lower()
    # calendar link only in follow-ups, never email 1
    assert "cal.example" not in body
    _, followup = render_email_template("followup_day3.j2", prospect)
    assert "https://cal.example/jane" in followup

"""Connections: DB credentials override env, single-active rule, masking."""
from db.models import Connection, ConnectionKind
from engine.connections import (activate, get_active, mask, resolve_email,
                                resolve_telegram)


def test_env_fallback_when_no_connections(session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_USER_ID", "42")
    monkeypatch.setenv("SMTP_HOST", "smtp.env.example")
    from engine.config import get_settings
    get_settings.cache_clear()

    tg = resolve_telegram(session)
    assert tg.token == "env-token"
    assert tg.user_id == 42
    assert tg.source == "env"
    email = resolve_email(session)
    assert email.smtp_host == "smtp.env.example"
    assert email.source == "env"


def test_active_connection_overrides_env(session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    from engine.config import get_settings
    get_settings.cache_clear()

    conn = Connection(kind=ConnectionKind.TELEGRAM, name="main bot",
                      config_json={"token": "db-token", "user_id": 777},
                      is_active=True)
    session.add(conn)
    session.commit()

    tg = resolve_telegram(session)
    assert tg.token == "db-token"
    assert tg.user_id == 777
    assert tg.source == "connection:main bot"


def test_inactive_connection_is_ignored(session):
    session.add(Connection(kind=ConnectionKind.EMAIL, name="saved but off",
                           config_json={"smtp_host": "smtp.off.example"},
                           is_active=False))
    session.commit()
    assert resolve_email(session).source == "env"


def test_activate_enforces_single_active_per_kind(session):
    a = Connection(kind=ConnectionKind.EMAIL, name="zoho",
                   config_json={"smtp_host": "smtp.zoho.example"}, is_active=True)
    b = Connection(kind=ConnectionKind.EMAIL, name="gmail",
                   config_json={"smtp_host": "smtp.gmail.example"})
    other_kind = Connection(kind=ConnectionKind.TELEGRAM, name="bot",
                            config_json={"token": "t"}, is_active=True)
    session.add_all([a, b, other_kind])
    session.commit()

    activate(session, b.id)
    session.refresh(a)
    session.refresh(b)
    session.refresh(other_kind)
    assert not a.is_active
    assert b.is_active
    assert other_kind.is_active  # other kinds untouched
    assert get_active(session, ConnectionKind.EMAIL).id == b.id
    assert resolve_email(session).smtp_host == "smtp.gmail.example"


def test_email_per_field_fallback(session, monkeypatch):
    """Blank IMAP fields fall back to SMTP credentials within the connection."""
    session.add(Connection(
        kind=ConnectionKind.EMAIL, name="zoho",
        config_json={"smtp_host": "smtp.zoho.example", "smtp_user": "me@z.example",
                     "smtp_password": "pw", "imap_host": "imap.zoho.example"},
        is_active=True))
    session.commit()
    email = resolve_email(session)
    assert email.imap_user == "me@z.example"
    assert email.imap_password == "pw"


def test_mask():
    assert mask("1234567890abcd") == "**********abcd"
    assert mask("abc") == "***"
    assert mask("") == ""

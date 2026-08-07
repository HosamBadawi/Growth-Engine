"""Connection resolution: saved DB connections (admin page) override .env.

n8n-style: credentials are stored once (Telegram bots, email accounts), one per
kind is active, and every consumer resolves through here. .env remains the
bootstrap fallback so nothing breaks with an empty connections table.
"""
import logging
import smtplib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Connection, ConnectionKind
from engine.config import get_settings

log = logging.getLogger("connections")


@dataclass
class TelegramConfig:
    token: str
    user_id: int
    source: str  # "connection:<name>" or "env"


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    imap_host: str
    imap_user: str
    imap_password: str
    from_name: str
    from_email: str
    postal_address: str
    source: str


def get_active(session: Session, kind: str) -> Connection | None:
    return session.execute(
        select(Connection).where(Connection.kind == kind, Connection.is_active.is_(True))
        .limit(1)
    ).scalar_one_or_none()


def activate(session: Session, connection_id: int) -> Connection | None:
    """Activate a connection. Exclusive kinds (Telegram/email/campaign) allow one
    active row; LLM connections may all be active at once because model roles
    reference them individually."""
    conn = session.get(Connection, connection_id)
    if not conn:
        return None
    exclusive = conn.kind != ConnectionKind.LLM
    if exclusive:
        for other in session.execute(
            select(Connection).where(Connection.kind == conn.kind)
        ).scalars().all():
            other.is_active = other.id == conn.id
    else:
        conn.is_active = True
    session.commit()
    return conn


def _own_session():
    from db.session import new_session

    return new_session()


def resolve_telegram(session: Session | None = None) -> TelegramConfig:
    settings = get_settings()
    own = session is None
    session = session or _own_session()
    try:
        conn = get_active(session, ConnectionKind.TELEGRAM)
    finally:
        if own:
            session.close()
    if conn:
        cfg = conn.config_json or {}
        try:
            user_id = int(cfg.get("user_id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        return TelegramConfig(
            token=cfg.get("token") or settings.telegram_bot_token,
            user_id=user_id or settings.telegram_user_id,
            source=f"connection:{conn.name}",
        )
    return TelegramConfig(settings.telegram_bot_token, settings.telegram_user_id, "env")


def resolve_email(session: Session | None = None) -> EmailConfig:
    settings = get_settings()
    own = session is None
    session = session or _own_session()
    try:
        conn = get_active(session, ConnectionKind.EMAIL)
    finally:
        if own:
            session.close()
    cfg = (conn.config_json or {}) if conn else {}

    def pick(key: str, env_value):
        return cfg.get(key) or env_value

    smtp_user = pick("smtp_user", settings.smtp_user)
    smtp_password = pick("smtp_password", settings.smtp_password)
    try:
        smtp_port = int(pick("smtp_port", settings.smtp_port) or 587)
    except (TypeError, ValueError):
        smtp_port = 587
    return EmailConfig(
        smtp_host=pick("smtp_host", settings.smtp_host),
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        imap_host=pick("imap_host", settings.imap_host),
        # Within a saved connection, blank IMAP fields fall back to that
        # connection's OWN SMTP account before any env value: mixing one
        # account's SMTP with another's IMAP would poll the wrong inbox.
        imap_user=(cfg.get("imap_user") or cfg.get("smtp_user")
                   or settings.imap_user or smtp_user),
        imap_password=(cfg.get("imap_password") or cfg.get("smtp_password")
                       or settings.imap_password or smtp_password),
        from_name=pick("from_name", settings.from_name),
        from_email=pick("from_email", settings.from_email),
        postal_address=pick("postal_address", settings.postal_address),
        source=f"connection:{conn.name}" if conn else "env",
    )


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    value = str(value)
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


# ── connection tests (called from the admin page) ────────────────────────────

async def test_telegram(token: str, user_id: int) -> tuple[bool, str]:
    """getMe + a test message to the owner id."""
    if not token:
        return False, "no token"
    try:
        from aiogram import Bot

        bot = Bot(token)
        try:
            me = await bot.get_me()
            detail = f"bot @{me.username} ok"
            if user_id:
                await bot.send_message(user_id,
                                       "Growth Engine: connection test successful.")
                detail += f", test message sent to {user_id}"
        finally:
            await bot.session.close()
        return True, detail
    except Exception as exc:  # noqa: BLE001 (surfaced verbatim in the admin UI)
        return False, str(exc)[:300]


def test_email_sync(cfg: EmailConfig) -> tuple[bool, str]:
    """SMTP login + IMAP login. Blocking, run via asyncio.to_thread."""
    parts = []
    if not cfg.smtp_host:
        return False, "no SMTP host"
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            server.starttls()
        with server:
            server.login(cfg.smtp_user, cfg.smtp_password)
        parts.append("SMTP login ok")
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP failed: {str(exc)[:200]}"

    if cfg.imap_host:
        try:
            from imap_tools import MailBox

            with MailBox(cfg.imap_host).login(cfg.imap_user, cfg.imap_password):
                parts.append("IMAP login ok")
        except Exception as exc:  # noqa: BLE001
            return False, f"SMTP ok but IMAP failed: {str(exc)[:200]}"
    else:
        parts.append("no IMAP host set (reply watcher will stay idle)")
    return True, ", ".join(parts)

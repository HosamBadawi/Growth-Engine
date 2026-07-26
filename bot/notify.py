"""One-way Telegram notifications from the engine (alerts, reports, reply cards).
Resolves the active Telegram connection (admin page) with .env fallback and
degrades to log-only when nothing is configured, so nothing else breaks."""
import logging

from engine.connections import resolve_telegram

log = logging.getLogger("notify")

_bot = None
_bot_token = None


def get_bot():
    """Bot for the currently active connection; rebuilt if the token changed."""
    global _bot, _bot_token
    token = resolve_telegram().token
    if not token:
        return None
    if _bot is None or _bot_token != token:
        from aiogram import Bot  # imported lazily so tests never need a token

        _bot = Bot(token)
        _bot_token = token
    return _bot


async def notify(text: str, reply_markup=None) -> bool:
    cfg = resolve_telegram()
    bot = get_bot()
    if not bot or not cfg.user_id:
        log.info("TG notify (not configured): %s", text[:300])
        return False
    try:
        await bot.send_message(
            cfg.user_id,
            text[:4000],
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 (alerting must never crash the engine)
        log.warning("Telegram notify failed: %s", exc)
        return False

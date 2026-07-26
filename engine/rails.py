"""Editable sending rails: the admin panel can only TIGHTEN them.

Overrides live in EngineState (key 'rail_overrides', JSON). Effective values are
override-or-env, then clamped to the hard code constants so a DB value can never
loosen a safety rail (the hard clamps in sender.py remain the last line too).

Loaded into a process cache so the hot send path pays no per-call DB cost;
invalidate() is called whenever the admin saves. Any DB/table error degrades to
"no overrides" so the tested clamp functions keep working without a database.
"""
import json
import logging

from engine.config import (HARD_MAX_DAILY, HARD_SEND_WINDOW_END,
                           HARD_SEND_WINDOW_START, get_settings)
from engine.util import parse_hhmm

log = logging.getLogger("rails")

STATE_KEY = "rail_overrides"
_cache: dict | None = None

# Absolute sanity ceiling for the bounce breaker rate (3% default; never looser).
HARD_BREAKER_RATE_MAX = 0.05


def invalidate() -> None:
    global _cache
    _cache = None


def get_overrides() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        from db.session import new_session
        from engine.state import get_state

        session = new_session()
        try:
            raw = get_state(session, STATE_KEY)
        finally:
            session.close()
        _cache = json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001 (no DB yet / bad JSON: behave as env-only)
        log.debug("rail overrides unavailable, using env defaults: %s", exc)
        _cache = {}
    return _cache


def save_overrides(session, data: dict) -> dict:
    """Validate + clamp, persist, invalidate cache. Returns the stored (clamped) dict."""
    from engine.state import set_state

    clean = clamp_overrides(data)
    set_state(session, STATE_KEY, json.dumps(clean))
    invalidate()
    return clean


def _hhmm_ok(value: str) -> bool:
    try:
        parse_hhmm(value)
        return True
    except (ValueError, AttributeError):
        return False


def clamp_overrides(data: dict) -> dict:
    """Keep only valid, tighten-only values."""
    settings = get_settings()
    out: dict = {}

    if "daily_send_cap" in data:
        try:
            out["daily_send_cap"] = max(1, min(int(data["daily_send_cap"]), HARD_MAX_DAILY))
        except (ValueError, TypeError):
            pass

    hard_start, hard_end = parse_hhmm(HARD_SEND_WINDOW_START), parse_hhmm(HARD_SEND_WINDOW_END)
    if _hhmm_ok(data.get("send_window_start", "")):
        if parse_hhmm(data["send_window_start"]) >= hard_start:  # can only start later
            out["send_window_start"] = data["send_window_start"]
    if _hhmm_ok(data.get("send_window_end", "")):
        if parse_hhmm(data["send_window_end"]) <= hard_end:  # can only end earlier
            out["send_window_end"] = data["send_window_end"]

    for key in ("jitter_min_minutes", "jitter_max_minutes"):
        if key in data:
            try:
                out[key] = max(0, int(data[key]))
            except (ValueError, TypeError):
                pass

    if "bounce_breaker_rate" in data:
        try:
            rate = float(data["bounce_breaker_rate"])
            # tighten-only: never above the env-configured rate, never above the ceiling
            out["bounce_breaker_rate"] = max(
                0.001, min(rate, settings.bounce_breaker_rate, HARD_BREAKER_RATE_MAX)
            )
        except (ValueError, TypeError):
            pass
    if "bounce_breaker_window" in data:
        try:  # a larger trailing window is stricter, so allow only >= env
            out["bounce_breaker_window"] = max(int(data["bounce_breaker_window"]),
                                               settings.bounce_breaker_window)
        except (ValueError, TypeError):
            pass
    return out


# ── effective values (override-or-env; hard clamps still applied downstream) ──

def eff_daily_cap() -> int:
    return int(get_overrides().get("daily_send_cap", get_settings().daily_send_cap))


def eff_window() -> tuple[str, str]:
    ov = get_overrides()
    settings = get_settings()
    return (ov.get("send_window_start", settings.send_window_start),
            ov.get("send_window_end", settings.send_window_end))


def eff_jitter() -> tuple[int, int]:
    ov = get_overrides()
    settings = get_settings()
    return (int(ov.get("jitter_min_minutes", settings.jitter_min_minutes)),
            int(ov.get("jitter_max_minutes", settings.jitter_max_minutes)))


def eff_breaker() -> tuple[float, int]:
    ov = get_overrides()
    settings = get_settings()
    return (float(ov.get("bounce_breaker_rate", settings.bounce_breaker_rate)),
            int(ov.get("bounce_breaker_window", settings.bounce_breaker_window)))

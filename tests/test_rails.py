"""Admin rails can only TIGHTEN: server-side clamps against the hard constants."""
import engine.rails as rails
from engine.config import HARD_MAX_DAILY
from engine.rails import clamp_overrides, save_overrides


def teardown_function():
    rails.invalidate()


def test_daily_cap_clamped_to_hard_max():
    assert clamp_overrides({"daily_send_cap": 999})["daily_send_cap"] == HARD_MAX_DAILY
    assert clamp_overrides({"daily_send_cap": 5})["daily_send_cap"] == 5
    assert clamp_overrides({"daily_send_cap": 0})["daily_send_cap"] == 1
    assert "daily_send_cap" not in clamp_overrides({"daily_send_cap": "junk"})


def test_window_can_only_narrow():
    # widening is silently dropped
    assert "send_window_start" not in clamp_overrides({"send_window_start": "07:00"})
    assert "send_window_end" not in clamp_overrides({"send_window_end": "20:00"})
    # narrowing is kept
    assert clamp_overrides({"send_window_start": "10:00"})["send_window_start"] == "10:00"
    assert clamp_overrides({"send_window_end": "15:00"})["send_window_end"] == "15:00"


def test_breaker_can_only_tighten(monkeypatch):
    from engine.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    # looser (higher) rate is clamped down to the env rate
    clamped = clamp_overrides({"bounce_breaker_rate": 0.5})
    assert clamped["bounce_breaker_rate"] <= settings.bounce_breaker_rate
    # tighter rate is kept
    assert clamp_overrides({"bounce_breaker_rate": 0.01})["bounce_breaker_rate"] == 0.01
    # smaller window (weaker) is raised to the env window
    assert (clamp_overrides({"bounce_breaker_window": 5})["bounce_breaker_window"]
            >= settings.bounce_breaker_window)


def test_saved_overrides_flow_into_effective_values(session):
    save_overrides(session, {"daily_send_cap": 7, "send_window_start": "11:00"})
    assert rails.eff_daily_cap() == 7
    assert rails.eff_window()[0] == "11:00"
    # and the sender's hard clamp still applies on top
    from engine.sender import effective_daily_cap
    assert effective_daily_cap(1, rails.eff_daily_cap()) == 7   # min(7, warmup 10)
    save_overrides(session, {"daily_send_cap": 30})
    assert effective_daily_cap(1, rails.eff_daily_cap()) == 10  # warmup still wins


def test_sender_window_respects_override(session):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from engine.sender import in_send_window

    def eastern(hour, minute):
        local = datetime(2026, 7, 20, hour, minute, tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    assert in_send_window(eastern(10, 0))
    save_overrides(session, {"send_window_start": "11:00"})
    assert not in_send_window(eastern(10, 0))   # tightened window now blocks 10:00
    assert in_send_window(eastern(11, 30))

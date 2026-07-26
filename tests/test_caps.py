"""Sending caps: warm-up ramp, hard maxima, window, jitter. Deliverability rails."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from engine.config import HARD_JITTER_MIN_SECONDS, HARD_MAX_DAILY, get_settings
from engine.sender import (compute_jitter_seconds, effective_daily_cap,
                           in_send_window, warmup_cap)


def test_warmup_schedule():
    assert warmup_cap(1) == 10
    assert warmup_cap(7) == 10
    assert warmup_cap(8) == 20
    assert warmup_cap(14) == 20
    assert warmup_cap(15) == 30
    assert warmup_cap(365) == HARD_MAX_DAILY


def test_env_cap_can_lower_but_never_raise():
    assert effective_daily_cap(1, 25) == 10      # warm-up wins
    assert effective_daily_cap(20, 25) == 25     # env lowers
    assert effective_daily_cap(20, 999) == HARD_MAX_DAILY  # env can NEVER raise
    assert effective_daily_cap(20, None) == HARD_MAX_DAILY
    assert effective_daily_cap(20, 0) == HARD_MAX_DAILY


def _utc_for_eastern(year, month, day, hour, minute) -> datetime:
    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def test_window_weekday_business_hours():
    # Mon 2026-07-20 10:00 ET -> inside
    assert in_send_window(_utc_for_eastern(2026, 7, 20, 10, 0))
    # Mon 08:59 ET -> outside
    assert not in_send_window(_utc_for_eastern(2026, 7, 20, 8, 59))
    # Mon 16:31 ET -> outside
    assert not in_send_window(_utc_for_eastern(2026, 7, 20, 16, 31))
    # Boundary 16:30 -> inside
    assert in_send_window(_utc_for_eastern(2026, 7, 20, 16, 30))


def test_window_weekend_blocked():
    # Sat 2026-07-18 and Sun 2026-07-19, midday ET
    assert not in_send_window(_utc_for_eastern(2026, 7, 18, 12, 0))
    assert not in_send_window(_utc_for_eastern(2026, 7, 19, 12, 0))


def test_jitter_bounds():
    settings = get_settings()
    lo = settings.jitter_min_minutes * 60
    hi = settings.jitter_max_minutes * 60
    for _ in range(200):
        j = compute_jitter_seconds()
        assert lo <= j <= hi
        assert j >= HARD_JITTER_MIN_SECONDS


def test_jitter_hard_floor(monkeypatch):
    monkeypatch.setenv("JITTER_MIN_MINUTES", "0")
    monkeypatch.setenv("JITTER_MAX_MINUTES", "1")
    get_settings.cache_clear()
    for _ in range(100):
        assert compute_jitter_seconds() >= HARD_JITTER_MIN_SECONDS


def test_volume_warmup_cannot_be_calendar_skipped():
    """A paused/idle stretch must not jump a cold domain to 30/day."""
    from engine.sender import volume_warmup_cap

    assert volume_warmup_cap(0) == 10
    assert volume_warmup_cap(69) == 10
    assert volume_warmup_cap(70) == 20
    assert volume_warmup_cap(209) == 20
    assert volume_warmup_cap(210) == HARD_MAX_DAILY
    # day 22 by calendar, but only 15 real sends ever -> still 10/day
    assert effective_daily_cap(22, 30, total_real_sent=15) == 10


def test_env_cannot_widen_send_window(monkeypatch):
    """SEND_WINDOW_* / SEND_TIMEZONE are clamped to 9:00-16:30 US Eastern."""
    monkeypatch.setenv("SEND_WINDOW_START", "00:00")
    monkeypatch.setenv("SEND_WINDOW_END", "23:59")
    get_settings.cache_clear()
    # Mon 3:00 ET stays blocked despite the widened env window
    assert not in_send_window(_utc_for_eastern(2026, 7, 20, 3, 0))
    assert in_send_window(_utc_for_eastern(2026, 7, 20, 10, 0))

    monkeypatch.setenv("SEND_TIMEZONE", "Asia/Tokyo")
    get_settings.cache_clear()
    # 10:00 Tokyo on a Monday is 21:00 Sunday ET -> hard rail blocks it
    from zoneinfo import ZoneInfo
    tokyo_monday = datetime(2026, 7, 20, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert not in_send_window(tokyo_monday.astimezone(timezone.utc).replace(tzinfo=None))

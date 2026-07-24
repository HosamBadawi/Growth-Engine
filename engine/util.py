"""Small shared helpers. All DB timestamps are naive UTC (SQLite returns naive)."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc_naive: datetime, tz_name: str) -> datetime:
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))

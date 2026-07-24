"""Key-value engine state stored in DB so it survives restarts."""
from sqlalchemy.orm import Session

from db.models import EngineState

KEY_PAUSED = "paused"
KEY_LIVE_CONFIRMED = "live_confirmed"
KEY_FIRST_SEND_DATE = "first_send_date"  # ISO date of first real (SANDBOX/LIVE) send
KEY_NEXT_SEND_AT = "next_send_at"        # ISO datetime gate for jitter between sends
KEY_BENCHMARKED = "benchmarked"


def get_state(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(EngineState, key)
    return row.value if row else default


def set_state(session: Session, key: str, value: str, commit: bool = True) -> None:
    row = session.get(EngineState, key)
    if row:
        row.value = value
    else:
        session.add(EngineState(key=key, value=value))
    if commit:
        session.commit()


def is_paused(session: Session) -> bool:
    return get_state(session, KEY_PAUSED, "0") == "1"


def set_paused(session: Session, paused: bool) -> None:
    set_state(session, KEY_PAUSED, "1" if paused else "0")


def is_live_confirmed(session: Session) -> bool:
    return get_state(session, KEY_LIVE_CONFIRMED, "0") == "1"

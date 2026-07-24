"""Activity log: every meaningful engine action lands in the events table."""
import logging

from sqlalchemy.orm import Session

from db.models import Event

log = logging.getLogger("events")


def log_event(
    session: Session,
    module: str,
    message: str,
    level: str = "INFO",
    meta: dict | None = None,
    commit: bool = True,
) -> None:
    session.add(Event(module=module, level=level, message=message, meta_json=meta or {}))
    if commit:
        session.commit()
    getattr(log, level.lower(), log.info)("[%s] %s", module, message)

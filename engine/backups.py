"""Nightly SQLite backup: copy the DB to data/backups/, keep the last 14.

Skips cleanly when DATABASE_URL is not SQLite (Postgres has its own tooling).
Uses the sqlite3 online backup API so it is safe while the engine is running.
"""
import logging
import sqlite3
from pathlib import Path

from engine.config import get_settings
from engine.util import utcnow

log = logging.getLogger("backups")

BACKUP_DIR = Path("data/backups")
KEEP = 14


def _sqlite_path() -> Path | None:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    raw = url.replace("sqlite:///", "", 1)
    if raw in ("", ":memory:"):
        return None
    return Path(raw)


def run_backup() -> str | None:
    """Create today's backup and prune old ones. Returns the path or None."""
    db_path = _sqlite_path()
    if db_path is None or not db_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"growth-{utcnow():%Y%m%d}.db"

    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    backups = sorted(BACKUP_DIR.glob("growth-*.db"))
    for old in backups[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    log.info("DB backup written: %s (%d kept)", dest.name, min(len(backups), KEEP))
    return str(dest)


async def nightly_backup() -> None:
    import asyncio

    from db.session import new_session
    from engine.events import log_event

    try:
        path = await asyncio.to_thread(run_backup)
    except Exception as exc:  # noqa: BLE001 (a backup failure must not kill the loop)
        log.warning("backup failed: %s", exc)
        session = new_session()
        try:
            log_event(session, "backups", f"Nightly backup FAILED: {exc}", level="ERROR")
        finally:
            session.close()
        return
    if path:
        session = new_session()
        try:
            log_event(session, "backups", f"Nightly backup written: {Path(path).name}")
        finally:
            session.close()

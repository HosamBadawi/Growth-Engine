"""Startup migrations: adopt Alembic on an existing DB, run upgrades on new ones.

- Fresh DB or a pre-Alembic DB (v1, or v2 created by create_all): create any
  missing tables (additive, safe, data untouched) and STAMP it at head.
- A DB already under Alembic control: run `upgrade head` so genuine future
  column/table migrations apply.

This keeps the operator's existing local database intact (migrations, never
resets) while making fresh clones boot with the full schema.
"""
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from db.session import get_engine, init_db

log = logging.getLogger("migrate")

_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "db" / "alembic"))
    return cfg


def run_migrations() -> None:
    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    cfg = _alembic_config()

    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    if current is not None:
        log.info("DB at Alembic revision %s, running upgrade head", current)
        command.upgrade(cfg, "head")
        return

    # No alembic_version yet.
    if tables - {"alembic_version"}:
        # Pre-Alembic database with real data: add any missing tables, adopt at
        # the BASELINE, then upgrade — so later revisions (and their data
        # backfills) still run instead of being stamped over.
        log.info("Adopting existing database under Alembic (baseline, then upgrade)")
        init_db()  # create_all — additive only, never drops
        command.stamp(cfg, "0001_baseline")
        command.upgrade(cfg, "head")
    else:
        # Brand new database: let Alembic build it from the baseline.
        log.info("Fresh database, running migrations to head")
        command.upgrade(cfg, "head")

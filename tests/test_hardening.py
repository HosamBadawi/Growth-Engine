"""Phase 6 hardening: migrations (adopt + fresh), backups with retention."""
import sqlite3

import pytest

import db.session as db_session
from engine.config import get_settings


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """Point the engine at a throwaway file DB and rebuild the global engine."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    old_engine, old_factory = db_session._engine, db_session._SessionLocal
    db_session.reset_engine_for_tests(None, None)
    yield db_path
    engine = db_session._engine
    if engine is not None:
        engine.dispose()
    db_session.reset_engine_for_tests(old_engine, old_factory)
    get_settings.cache_clear()


def _tables(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'")}
    finally:
        conn.close()


def test_migrations_build_fresh_db(file_db):
    from db.migrate import run_migrations

    run_migrations()
    tables = _tables(file_db)
    for expected in ("prospects", "touches", "connections", "model_roles",
                     "llm_usage", "alembic_version"):
        assert expected in tables, expected


def test_migrations_adopt_existing_db_without_data_loss(file_db):
    """A pre-Alembic database (v1-style create_all) is adopted in place."""
    from db.migrate import run_migrations
    from db.models import Prospect
    from db.session import init_db, new_session

    init_db()  # simulate the old create_all world (no alembic_version)
    session = new_session()
    session.add(Prospect(name="Keep Me Plumbing", trade="plumber"))
    session.commit()
    session.close()
    assert "alembic_version" not in _tables(file_db)

    run_migrations()  # adoption path
    assert "alembic_version" in _tables(file_db)

    run_migrations()  # second run: upgrade head, must be a no-op
    session = new_session()
    try:
        assert session.query(Prospect).filter_by(name="Keep Me Plumbing").count() == 1
    finally:
        session.close()


def test_backup_creates_file_and_prunes(file_db, tmp_path, monkeypatch):
    import engine.backups as backups
    from db.migrate import run_migrations

    run_migrations()
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backups, "BACKUP_DIR", backup_dir)

    path = backups.run_backup()
    assert path and backup_dir.exists()
    assert len(list(backup_dir.glob("growth-*.db"))) == 1

    # retention: 20 fakes + today's -> only KEEP remain after the next run
    for i in range(20):
        (backup_dir / f"growth-2020{i:04d}.db").write_bytes(b"x")
    backups.run_backup()
    assert len(list(backup_dir.glob("growth-*.db"))) == backups.KEEP


def test_backup_skips_non_sqlite(monkeypatch):
    import engine.backups as backups

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    get_settings.cache_clear()
    assert backups.run_backup() is None
    get_settings.cache_clear()


def test_version_is_single_source():
    from engine import __version__

    assert __version__ == "2.0.0"

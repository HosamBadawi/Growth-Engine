"""Engine + session factory. SQLite now; swap DATABASE_URL for Postgres later."""
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base
from engine.config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            db_path = url.replace("sqlite:///", "", 1)
            if db_path != ":memory:":
                Path(os.path.dirname(db_path) or ".").mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def new_session() -> Session:
    return get_session_factory()()


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def reset_engine_for_tests(engine, factory) -> None:
    global _engine, _SessionLocal
    _engine = engine
    _SessionLocal = factory

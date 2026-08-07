"""Shared fixtures: in-memory DB, safe default settings."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.session as db_session
from db.models import Base, Prospect, ProspectStatus, VerificationLevel
from engine.config import get_settings


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    """Every test runs in DRY_RUN with deterministic settings.

    OLLAMA_BASE_URL points at a dead port on purpose: the suite must be fully
    offline-safe (CI has no services), so any test that accidentally depends on
    a live LLM fails immediately instead of passing only on a dev machine.
    """
    monkeypatch.setenv("ENGINE_MODE", "DRY_RUN")
    monkeypatch.setenv("SMTP_PROBE_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SEND_TIMEZONE", "America/New_York")
    monkeypatch.setenv("SEND_WINDOW_START", "09:00")
    monkeypatch.setenv("SEND_WINDOW_END", "16:30")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # dead port
    get_settings.cache_clear()
    import engine.campaign as campaign_mod

    campaign_mod._warned_fields = ()  # warn-once dedupe must not leak across tests
    yield
    get_settings.cache_clear()


@pytest.fixture
def session():
    """Fresh in-memory database per test, wired into db.session for module code."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    old_engine, old_factory = db_session._engine, db_session._SessionLocal
    db_session.reset_engine_for_tests(engine, factory)

    import engine.prospector_settings as prospector_settings
    import engine.rails as rails

    rails.invalidate()
    prospector_settings.invalidate()
    sess = factory()
    yield sess
    sess.close()
    rails.invalidate()
    prospector_settings.invalidate()
    db_session.reset_engine_for_tests(old_engine, old_factory)


@pytest.fixture
def prospect(session):
    p = Prospect(
        name="Tampa Bay Plumbing Pros",
        trade="plumber",
        city="tampa",
        state="FL",
        website="https://tampabayplumbingpros.example",
        email="mike@tampabayplumbingpros.example",
        email_verification_level=VerificationLevel.MX,
        owner_name="Mike Rivera",
        status=ProspectStatus.VERIFIED,
        rating=4.8,
        review_count=57,
        intel_json={"years_in_business": "since 2009", "has_chat_widget": False},
    )
    session.add(p)
    session.commit()
    return p

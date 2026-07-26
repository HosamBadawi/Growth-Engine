"""SQLAlchemy 2.0 models. SQLite now, Postgres later (no SQLite-only types)."""
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from engine.util import utcnow


class Base(DeclarativeBase):
    pass


class ProspectStatus:
    NEW = "NEW"
    ENRICHED = "ENRICHED"
    VERIFIED = "VERIFIED"
    DRAFTED = "DRAFTED"
    QUEUED = "QUEUED"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    CALL_BOOKED = "CALL_BOOKED"
    WON = "WON"
    LOST = "LOST"
    SUPPRESSED = "SUPPRESSED"
    FORM_ONLY = "FORM_ONLY"

    ALL = [NEW, ENRICHED, VERIFIED, DRAFTED, QUEUED, CONTACTED, REPLIED,
           INTERESTED, CALL_BOOKED, WON, LOST, SUPPRESSED, FORM_ONLY]


class VerificationLevel:
    NONE = "NONE"
    SYNTAX = "SYNTAX"
    MX = "MX"
    SMTP_OK = "SMTP_OK"
    FAILED = "FAILED"


class TouchType:
    EMAIL_1 = "EMAIL_1"
    FOLLOWUP_2 = "FOLLOWUP_2"
    FOLLOWUP_3 = "FOLLOWUP_3"
    BREAKUP = "BREAKUP"
    FORM = "FORM"
    MANUAL = "MANUAL"
    REPLY_OUT = "REPLY_OUT"

    SEQUENCE = [EMAIL_1, FOLLOWUP_2, FOLLOWUP_3, BREAKUP]
    FOLLOWUP_OFFSETS_DAYS = {FOLLOWUP_2: 3, FOLLOWUP_3: 6, BREAKUP: 10}


class TouchStatus:
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    PAUSED = "PAUSED"
    SENT = "SENT"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class ReplyClass:
    INTERESTED = "INTERESTED"
    QUESTION = "QUESTION"
    NOT_NOW = "NOT_NOW"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    AUTO_REPLY = "AUTO_REPLY"
    BOUNCE = "BOUNCE"

    ALL = [INTERESTED, QUESTION, NOT_NOW, UNSUBSCRIBE, AUTO_REPLY, BOUNCE]


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    trade: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    state: Mapped[Optional[str]] = mapped_column(String(50))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    website: Mapped[Optional[str]] = mapped_column(String(500))
    maps_url: Mapped[Optional[str]] = mapped_column(String(1000))
    rating: Mapped[Optional[float]] = mapped_column(Float)
    review_count: Mapped[Optional[int]] = mapped_column(Integer)
    owner_name: Mapped[Optional[str]] = mapped_column(String(255))
    # Cheap provider-side identity (license no, else name|city) so a /find run
    # can exclude already-known businesses BEFORE paying for website discovery.
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(320), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    email_verification_level: Mapped[str] = mapped_column(
        String(20), default=VerificationLevel.NONE
    )
    contact_form_url: Mapped[Optional[str]] = mapped_column(String(1000))
    source: Mapped[str] = mapped_column(String(50), default="gosom")
    intel_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=ProspectStatus.NEW, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    touches: Mapped[list["Touch"]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan", order_by="Touch.id"
    )
    replies: Mapped[list["Reply"]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan", order_by="Reply.id"
    )


class Touch(Base):
    __tablename__ = "touches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    body: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=TouchStatus.DRAFT, index=True)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)

    prospect: Mapped[Prospect] = relationship(back_populates="touches")


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    classification: Mapped[str] = mapped_column(String(20))
    raw_text: Mapped[str] = mapped_column(Text)
    suggested_reply: Mapped[Optional[str]] = mapped_column(Text)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)

    prospect: Mapped[Prospect] = relationship(back_populates="replies")


class Suppression(Base):
    __tablename__ = "suppression"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    reason: Mapped[str] = mapped_column(String(255))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    module: Mapped[str] = mapped_column(String(50), index=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ConnectionKind:
    TELEGRAM = "TELEGRAM"
    EMAIL = "EMAIL"
    LLM = "LLM"
    CAMPAIGN = "CAMPAIGN"

    ALL = [TELEGRAM, EMAIL, LLM, CAMPAIGN]


class Connection(Base):
    """Saved, reusable credentials/config (n8n style): Telegram bots, email
    accounts, LLM providers, campaign profiles. One row per kind is active at a
    time; .env values are the fallback."""

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(100))
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class EngineState(Base):
    """Simple key-value store for engine runtime state (paused, warm-up start, ...)."""

    __tablename__ = "engine_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ModelRole(Base):
    """Which LLM serves each role (writer / classifier / researcher).
    llm_connection_id NULL = the default local Ollama; no row at all = .env model."""

    __tablename__ = "model_roles"

    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    llm_connection_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("connections.id"), nullable=True
    )
    model_name: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LlmUsage(Base):
    """One row per LLM call: powers the API cost guard and the /models report."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider_label: Mapped[str] = mapped_column(String(100), index=True)
    provider_type: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)

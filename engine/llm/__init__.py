"""Multi-provider LLM layer. The only entry points call sites should use:

    await llm_chat_json(role, system, user, required_keys=[...])
    await llm_chat_text(role, system, user)

Roles (writer / classifier / researcher) resolve to a provider + model via the
model_roles table -> saved "LLM" connection -> .env local-Ollama defaults, so a
fresh install with no DB rows behaves exactly like v1: local Ollama + env models.

API-key providers are cost-guarded: API_DAILY_CALL_CAP calls per provider per
local day, then automatic fallback to the local Ollama role default for the
rest of the day. The machine never silently burns money and never stops working.
"""
import asyncio
import logging
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.config import get_settings
from engine.llm.anthropic import AnthropicProvider
from engine.llm.base import LLMError, LLMProvider
from engine.llm.ollama import OllamaProvider
from engine.llm.openai_compat import OpenAICompatProvider

log = logging.getLogger("llm")

ROLES = ("writer", "classifier", "researcher")

PROVIDER_TYPES = {
    "ollama": OllamaProvider,
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}

_cap_alerted_on: str | None = None  # local date we already alerted for


def _env_model_for(role: str) -> str:
    settings = get_settings()
    return {
        "writer": settings.writer_model,
        "classifier": settings.classifier_model,
        "researcher": settings.effective_researcher_model,
    }[role]


def _local_ollama() -> OllamaProvider:
    return OllamaProvider(base_url=get_settings().ollama_base_url, label="local ollama")


def build_provider(provider_type: str, base_url: str = "", api_key: str = "",
                   label: str = "") -> LLMProvider:
    cls = PROVIDER_TYPES.get(provider_type)
    if cls is None:
        raise LLMError(f"Unknown LLM provider_type '{provider_type}', "
                       f"options: {list(PROVIDER_TYPES)}")
    return cls(base_url=base_url, api_key=api_key, label=label)


def provider_from_connection(conn) -> LLMProvider:
    cfg = conn.config_json or {}
    return build_provider(
        cfg.get("provider_type", "ollama"),
        base_url=cfg.get("base_url", ""),
        api_key=cfg.get("api_key", ""),
        label=conn.name,
    )


def _today_utc_bounds() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = datetime.combine(now.date(), time.min)
    return start, now


def api_calls_today(session: Session, provider_label: str) -> int:
    from db.models import LlmUsage

    start, _ = _today_utc_bounds()
    return session.execute(
        select(func.count(LlmUsage.id)).where(
            LlmUsage.provider_label == provider_label,
            LlmUsage.provider_type != "ollama",
            LlmUsage.ts >= start,
        )
    ).scalar() or 0


async def _alert_cap_breach(label: str) -> None:
    global _cap_alerted_on
    today = datetime.now(timezone.utc).date().isoformat()
    if _cap_alerted_on == today:
        return
    _cap_alerted_on = today
    from bot.notify import notify

    await notify(
        f"LLM cost guard: provider '{label}' hit API_DAILY_CALL_CAP "
        f"({get_settings().api_daily_call_cap} calls today). Falling back to "
        f"local Ollama until tomorrow."
    )


async def resolve_role(role: str, session: Session) -> tuple[LLMProvider, str]:
    """role -> (provider, model). DB assignment wins; env local Ollama otherwise."""
    from db.models import Connection, ModelRole

    if role not in ROLES:
        raise LLMError(f"Unknown LLM role '{role}', options: {ROLES}")
    settings = get_settings()
    row = session.get(ModelRole, role)
    if row is None:
        return _local_ollama(), _env_model_for(role)

    model = row.model_name or _env_model_for(role)
    if row.llm_connection_id is None:
        return _local_ollama(), model

    conn = session.get(Connection, row.llm_connection_id)
    if conn is None or not conn.is_active:
        log.warning("Role %s points at missing/disabled connection, using local default", role)
        return _local_ollama(), _env_model_for(role)

    provider = provider_from_connection(conn)
    if provider.provider_type != "ollama":
        if api_calls_today(session, provider.label) >= settings.api_daily_call_cap:
            from engine.events import log_event

            log_event(session, "llm",
                      f"API daily cap reached for '{provider.label}', "
                      f"falling back to local Ollama for role {role}",
                      level="WARNING")
            await _alert_cap_breach(provider.label)
            return _local_ollama(), _env_model_for(role)
    return provider, model


def _record_usage(session: Session, provider: LLMProvider, model: str,
                  role: str) -> None:
    from db.models import LlmUsage

    usage = provider.last_usage or {}
    session.add(LlmUsage(
        provider_label=provider.label or provider.provider_type,
        provider_type=provider.provider_type,
        model=model,
        role=role,
        tokens_in=int(usage.get("input", 0) or 0),
        tokens_out=int(usage.get("output", 0) or 0),
    ))
    session.commit()


async def _call_with_fallback(role: str, method: str, **kwargs):
    """Resolve, call, record usage; on API-provider failure retry once locally."""
    from db.session import new_session

    session = new_session()
    try:
        provider, model = await resolve_role(role, session)
        try:
            result = await getattr(provider, method)(model, **kwargs)
            _record_usage(session, provider, model, role)
            return result
        except LLMError:
            if provider.provider_type == "ollama":
                raise
            from engine.events import log_event

            log_event(session, "llm",
                      f"Provider '{provider.label}' failed for role {role}, "
                      f"falling back to local Ollama", level="WARNING")
            fallback, fb_model = _local_ollama(), _env_model_for(role)
            result = await getattr(fallback, method)(fb_model, **kwargs)
            _record_usage(session, fallback, fb_model, role)
            return result
    finally:
        session.close()


async def llm_chat_json(role: str, system: str, user: str,
                        required_keys: list[str], max_attempts: int = 3,
                        temperature: float = 0.4) -> dict:
    return await _call_with_fallback(
        role, "chat_json", system=system, user=user,
        required_keys=required_keys, max_attempts=max_attempts,
        temperature=temperature,
    )


async def llm_chat_text(role: str, system: str, user: str,
                        temperature: float = 0.4) -> str:
    return await _call_with_fallback(
        role, "chat_text", system=system, user=user, temperature=temperature,
    )


def usage_today_summary(session: Session) -> dict:
    """Per-provider call counts today (for /models and the admin page)."""
    from db.models import LlmUsage

    start, _ = _today_utc_bounds()
    rows = session.execute(
        select(LlmUsage.provider_label, LlmUsage.provider_type,
               func.count(LlmUsage.id), func.sum(LlmUsage.tokens_in),
               func.sum(LlmUsage.tokens_out))
        .where(LlmUsage.ts >= start)
        .group_by(LlmUsage.provider_label, LlmUsage.provider_type)
    ).all()
    return {
        label: {"provider_type": ptype, "calls": calls,
                "tokens_in": tin or 0, "tokens_out": tout or 0}
        for label, ptype, calls, tin, tout in rows
    }

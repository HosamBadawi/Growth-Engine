"""Prospector knobs editable from the admin panel (override-or-env).

Same pattern as engine/rails.py: overrides live in EngineState as JSON, cached
per process, .env is the fallback. Nothing here is a safety rail, so values are
sanity-checked but not clamped against hard constants.
"""
import json
import logging

from engine.config import get_settings

log = logging.getLogger("prospector.settings")

STATE_KEY = "prospector_overrides"
VALID_PROVIDERS = ("registry", "gosom", "csv")
_cache: dict | None = None


def invalidate() -> None:
    global _cache
    _cache = None


def get_overrides() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        from db.session import new_session
        from engine.state import get_state

        session = new_session()
        try:
            raw = get_state(session, STATE_KEY)
        finally:
            session.close()
        _cache = json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001 — no DB yet: behave as env-only
        log.debug("prospector overrides unavailable: %s", exc)
        _cache = {}
    return _cache


def save_overrides(session, data: dict) -> dict:
    from engine.state import set_state

    clean: dict = {}
    if data.get("provider") in VALID_PROVIDERS:
        clean["provider"] = data["provider"]
    if data.get("registry_state"):
        clean["registry_state"] = str(data["registry_state"]).strip().upper()[:2]
    for key in ("min_review_count", "max_review_count"):
        if data.get(key) not in (None, ""):
            try:
                clean[key] = max(0, int(data[key]))
            except (ValueError, TypeError):
                pass
    if "franchise_keywords" in data:
        keywords = [k.strip().lower() for k in str(data["franchise_keywords"]).split(",")
                    if k.strip()]
        clean["franchise_keywords"] = ",".join(keywords)
    set_state(session, STATE_KEY, json.dumps(clean))
    invalidate()
    return clean


def eff_provider() -> str:
    return get_overrides().get("provider", get_settings().prospect_provider)


def eff_registry_state() -> str:
    return get_overrides().get("registry_state",
                               get_settings().registry_default_state).upper()


def eff_review_bounds() -> tuple[int, int]:
    ov = get_overrides()
    settings = get_settings()
    return (int(ov.get("min_review_count", settings.min_review_count)),
            int(ov.get("max_review_count", settings.max_review_count)))


def eff_franchise_keywords() -> list[str]:
    ov = get_overrides()
    if "franchise_keywords" in ov:
        return [k for k in ov["franchise_keywords"].split(",") if k]
    return get_settings().franchise_keyword_list

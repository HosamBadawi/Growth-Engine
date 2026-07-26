"""Backward-compatible shim over engine.llm, so v1 call sites keep working.

New code should use engine.llm (llm_chat_json / llm_chat_text / resolve_role).
"""
from engine.config import get_settings
from engine.llm.base import LLMError
from engine.llm.ollama import OllamaProvider

# v1 name for the same failure class
OllamaError = LLMError


def _provider() -> OllamaProvider:
    return OllamaProvider(base_url=get_settings().ollama_base_url, label="local ollama")


async def list_models() -> list[str]:
    return await _provider().list_models()


async def chat(model: str, system: str, user: str, json_mode: bool = False,
               temperature: float = 0.4) -> str:
    return await _provider()._chat_raw(model, system, user, json_mode=json_mode,
                                       temperature=temperature)


async def chat_json(model: str, system: str, user: str, required_keys: list[str],
                    max_attempts: int = 3, temperature: float = 0.4) -> dict:
    return await _provider().chat_json(model, system, user, required_keys,
                                       max_attempts=max_attempts,
                                       temperature=temperature)


async def benchmark(model: str) -> float:
    return await _provider().benchmark(model)


async def startup_check() -> list[str]:
    """Verify configured local models exist. Returns human-readable problems."""
    settings = get_settings()
    try:
        installed = await list_models()
    except LLMError as exc:
        return [str(exc)]

    def has(name: str) -> bool:
        return any(m == name or m.split(":")[0] == name for m in installed)

    problems = []
    for label, model in (("WRITER_MODEL", settings.writer_model),
                         ("CLASSIFIER_MODEL", settings.classifier_model)):
        if not has(model):
            problems.append(
                f"{label}={model} is not installed in Ollama. Run: ollama pull {model} "
                f"(installed: {', '.join(installed) or 'none'})"
            )
    return problems

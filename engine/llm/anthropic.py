"""Anthropic provider — POST /v1/messages. max_tokens is REQUIRED by that API;
system is a top-level field, not a message; JSON via prompt-and-validate (the
base class handles retries). Model listing uses GET /v1/models.
"""
from engine.llm.base import LLMError, LLMProvider

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024


class AnthropicProvider(LLMProvider):
    provider_type = "anthropic"

    @classmethod
    def default_base_url(cls) -> str:
        return "https://api.anthropic.com/v1"

    def _headers(self) -> dict:
        return {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    async def _chat_raw(self, model: str, system: str, user: str,
                        json_mode: bool, temperature: float) -> str:
        if json_mode:
            system = (system + "\n\nReply with ONLY a valid JSON object, "
                      "no prose, no code fences.")
        payload = {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
        }
        data = await self._post(f"{self.base_url}/messages", payload,
                                headers=self._headers())
        usage = data.get("usage") or {}
        self.last_usage = {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        }
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text:
            raise LLMError(f"{self.label}: empty completion")
        return text

    async def list_models(self) -> list[str]:
        data = await self._get(f"{self.base_url}/models", headers=self._headers())
        items = data.get("data") if isinstance(data, dict) else data
        return [m.get("id", "") for m in (items or []) if m.get("id")]

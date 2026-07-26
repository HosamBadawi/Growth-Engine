"""Ollama provider — wraps the native /api/chat + /api/tags endpoints.

Absorbs the v1 ollama_client logic (benchmark + startup_check stay available via
engine.ollama_client, which now delegates here) so nothing local regressed.
"""
from engine.config import get_settings
from engine.llm.base import LLMProvider


class OllamaProvider(LLMProvider):
    provider_type = "ollama"

    @classmethod
    def default_base_url(cls) -> str:
        return get_settings().ollama_base_url

    async def _chat_raw(self, model: str, system: str, user: str,
                        json_mode: bool, temperature: float) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"
        data = await self._post(f"{self.base_url}/api/chat", payload, timeout=300.0)
        self.last_usage = {
            "input": data.get("prompt_eval_count", 0),
            "output": data.get("eval_count", 0),
        }
        return data.get("message", {}).get("content", "")

    async def list_models(self) -> list[str]:
        data = await self._get(f"{self.base_url}/api/tags")
        return [m["name"] for m in data.get("models", [])]

    async def benchmark(self, model: str) -> float:
        """Generation tokens/sec for a short prompt (startup tuning)."""
        data = await self._post(
            f"{self.base_url}/api/generate",
            {"model": model,
             "prompt": "Write exactly two short sentences about plumbing.",
             "stream": False},
            timeout=600.0,
        )
        eval_count = data.get("eval_count", 0)
        eval_duration = data.get("eval_duration", 1)  # nanoseconds
        return round(eval_count / (eval_duration / 1e9), 1) if eval_count else 0.0

"""LLMProvider ABC. The JSON-retry discipline that v1 proved out lives here so
every provider (local or API) inherits identical validation and retry behavior.

Research note (per repo rule 6): raw httpx over the openai/anthropic SDKs —
both APIs are single-POST JSON, the SDKs would add two dependency trees for a
few dozen lines, and httpx is already a core dependency.
"""
import json
import logging
import re
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    """Any provider failure: unreachable, auth, bad payload, invalid JSON output."""


class LLMProvider(ABC):
    provider_type = "base"

    def __init__(self, base_url: str = "", api_key: str = "", label: str = ""):
        self.base_url = (base_url or self.default_base_url()).rstrip("/")
        self.api_key = api_key
        self.label = label or self.provider_type
        self.last_usage: dict | None = None  # {"input": n, "output": n} after a call

    @classmethod
    def default_base_url(cls) -> str:
        return ""

    # Single HTTP seam — tests monkeypatch this instead of mocking httpx.
    async def _post(self, url: str, payload: dict, headers: dict | None = None,
                    timeout: float = 60.0) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers or {})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"{self.label}: HTTP {exc.response.status_code} from {url}: "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.label}: cannot reach {url}: {exc}") from exc

    async def _get(self, url: str, headers: dict | None = None,
                   timeout: float = 15.0) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers or {})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"{self.label}: HTTP {exc.response.status_code} from {url}: "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.label}: cannot reach {url}: {exc}") from exc

    @abstractmethod
    async def _chat_raw(self, model: str, system: str, user: str,
                        json_mode: bool, temperature: float) -> str:
        """One completion; must set self.last_usage when the API reports tokens."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Model names this provider can serve."""

    async def chat_text(self, model: str, system: str, user: str,
                        temperature: float = 0.4) -> str:
        return await self._chat_raw(model, system, user, json_mode=False,
                                    temperature=temperature)

    async def chat_json(self, model: str, system: str, user: str,
                        required_keys: list[str], max_attempts: int = 3,
                        temperature: float = 0.4) -> dict:
        """Strict-JSON chat with validation and error-feedback retries."""
        prompt = user
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            raw = await self._chat_raw(model, system, prompt, json_mode=True,
                                       temperature=temperature)
            try:
                data = json.loads(_strip_fences(raw))
                missing = [k for k in required_keys
                           if k not in data or data[k] in (None, "")]
                if missing:
                    raise ValueError(f"missing keys: {missing}")
                return data
            except (json.JSONDecodeError, ValueError) as exc:
                last_err = str(exc)
                log.warning("%s: invalid JSON from %s (attempt %d/%d): %s",
                            self.label, model, attempt, max_attempts, exc)
                prompt = (
                    f"{user}\n\nYour previous output was invalid ({last_err}). "
                    f"Reply with ONLY a valid JSON object containing keys: "
                    f"{required_keys}."
                )
        raise LLMError(f"{self.label}: model {model} failed to produce valid JSON "
                       f"after {max_attempts} attempts: {last_err}")

    async def test(self) -> tuple[bool, str]:
        """Auth + connectivity + one tiny completion. Used by the admin Test button."""
        try:
            models = await self.list_models()
            probe = models[0] if models else ""
            if probe:
                await self._chat_raw(probe, "Reply with the single word: ok",
                                     "ok?", json_mode=False, temperature=0.0)
            return True, f"{len(models)} models available" + (
                f", completion ok on {probe}" if probe else "")
        except LLMError as exc:
            return False, str(exc)[:300]


def _strip_fences(raw: str) -> str:
    """API models sometimes wrap JSON in ```json fences despite instructions."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()

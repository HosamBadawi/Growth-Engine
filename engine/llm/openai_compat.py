"""OpenAI-compatible provider. One class covers OpenAI/ChatGPT, OpenRouter,
Groq, DeepSeek, Together, LM Studio, and even Ollama's own /v1 endpoint: any
server speaking POST /chat/completions.
"""
from engine.llm.base import LLMError, LLMProvider


class OpenAICompatProvider(LLMProvider):
    provider_type = "openai_compat"

    @classmethod
    def default_base_url(cls) -> str:
        return "https://api.openai.com/v1"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _chat_raw(self, model: str, system: str, user: str,
                        json_mode: bool, temperature: float) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            # Widely supported; servers that don't understand it ignore the field,
            # and the base-class JSON validator still guards the output.
            payload["response_format"] = {"type": "json_object"}
        data = await self._post(f"{self.base_url}/chat/completions", payload,
                                headers=self._headers())
        usage = data.get("usage") or {}
        self.last_usage = {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
        }
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.label}: no choices in response")
        return choices[0].get("message", {}).get("content", "")

    async def list_models(self) -> list[str]:
        data = await self._get(f"{self.base_url}/models", headers=self._headers())
        items = data.get("data") if isinstance(data, dict) else data
        return [m.get("id", "") for m in (items or []) if m.get("id")]

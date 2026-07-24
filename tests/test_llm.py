"""LLM layer: provider request/response shapes (mocked), JSON retry discipline,
role resolution, zero-config behavior, and the API cost-cap fallback."""
import pytest

from db.models import Connection, ConnectionKind, LlmUsage, ModelRole
from engine.config import get_settings
from engine.llm import (api_calls_today, build_provider, resolve_role,
                        usage_today_summary)
from engine.llm.anthropic import AnthropicProvider
from engine.llm.base import LLMError, LLMProvider, _strip_fences
from engine.llm.ollama import OllamaProvider
from engine.llm.openai_compat import OpenAICompatProvider


# ── base helpers ─────────────────────────────────────────────────────────────

def test_strip_fences():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_fences('{"a": 1}') == '{"a": 1}'
    assert _strip_fences("") == ""


class FakeProvider(LLMProvider):
    provider_type = "fake"

    def __init__(self, replies: list[str], **kw):
        super().__init__(**kw)
        self.replies = list(replies)
        self.calls = 0

    async def _chat_raw(self, model, system, user, json_mode, temperature):
        self.calls += 1
        if not self.replies:
            raise LLMError("fake: exhausted")
        return self.replies.pop(0)

    async def list_models(self):
        return ["fake-model"]


async def test_chat_json_retries_then_succeeds():
    provider = FakeProvider(["not json at all", '{"subject": "s"}',
                             '{"subject": "s", "body": "b"}'])
    data = await provider.chat_json("m", "sys", "user",
                                    required_keys=["subject", "body"])
    assert data == {"subject": "s", "body": "b"}
    assert provider.calls == 3


async def test_chat_json_gives_up_with_llmerror():
    provider = FakeProvider(["nope", "still nope", "never"])
    with pytest.raises(LLMError):
        await provider.chat_json("m", "sys", "user", required_keys=["x"],
                                 max_attempts=3)


# ── provider request/response shapes (single HTTP seam monkeypatched) ────────

async def test_openai_compat_shapes(monkeypatch):
    provider = OpenAICompatProvider(base_url="https://api.example/v1",
                                    api_key="sk-test", label="test-oai")
    captured = {}

    async def fake_post(url, payload, headers=None, timeout=60.0):
        captured.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7}}

    monkeypatch.setattr(provider, "_post", fake_post)
    text = await provider._chat_raw("gpt-test", "sys", "user",
                                    json_mode=True, temperature=0.2)
    assert text == '{"ok": true}'
    assert captured["url"] == "https://api.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["payload"]["model"] == "gpt-test"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "sys"}
    assert provider.last_usage == {"input": 11, "output": 7}


async def test_anthropic_shapes(monkeypatch):
    provider = AnthropicProvider(api_key="sk-ant-test", label="test-claude")
    captured = {}

    async def fake_post(url, payload, headers=None, timeout=60.0):
        captured.update(url=url, payload=payload, headers=headers)
        return {"content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 9, "output_tokens": 3}}

    monkeypatch.setattr(provider, "_post", fake_post)
    text = await provider._chat_raw("claude-test", "sys", "user",
                                    json_mode=False, temperature=0.2)
    assert text == "hello"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "max_tokens" in captured["payload"]  # REQUIRED by the API
    assert captured["payload"]["system"] == "sys"  # top-level, not a message
    assert provider.last_usage == {"input": 9, "output": 3}


def test_build_provider_types():
    assert isinstance(build_provider("ollama"), OllamaProvider)
    assert isinstance(build_provider("openai_compat"), OpenAICompatProvider)
    assert isinstance(build_provider("anthropic"), AnthropicProvider)
    with pytest.raises(LLMError):
        build_provider("nonsense")


# ── role resolution ──────────────────────────────────────────────────────────

async def test_zero_config_resolves_to_env_ollama(session):
    provider, model = await resolve_role("writer", session)
    assert provider.provider_type == "ollama"
    assert model == get_settings().writer_model


async def test_role_assignment_resolves_connection(session):
    conn = Connection(kind=ConnectionKind.LLM, name="my openrouter",
                      config_json={"provider_type": "openai_compat",
                                   "base_url": "https://openrouter.example/v1",
                                   "api_key": "sk-or"},
                      is_active=True)
    session.add(conn)
    session.commit()
    session.add(ModelRole(role="writer", llm_connection_id=conn.id,
                          model_name="meta-llama/llama-3.3-70b"))
    session.commit()

    provider, model = await resolve_role("writer", session)
    assert provider.provider_type == "openai_compat"
    assert provider.label == "my openrouter"
    assert model == "meta-llama/llama-3.3-70b"


async def test_disabled_connection_falls_back_to_env(session):
    conn = Connection(kind=ConnectionKind.LLM, name="off",
                      config_json={"provider_type": "anthropic", "api_key": "k"},
                      is_active=False)
    session.add(conn)
    session.commit()
    session.add(ModelRole(role="classifier", llm_connection_id=conn.id,
                          model_name="claude-x"))
    session.commit()

    provider, model = await resolve_role("classifier", session)
    assert provider.provider_type == "ollama"
    assert model == get_settings().classifier_model


async def test_unknown_role_raises(session):
    with pytest.raises(LLMError):
        await resolve_role("poet", session)


# ── cost guard ───────────────────────────────────────────────────────────────

async def test_api_cap_falls_back_to_local(session, monkeypatch):
    monkeypatch.setenv("API_DAILY_CALL_CAP", "3")
    get_settings.cache_clear()

    conn = Connection(kind=ConnectionKind.LLM, name="pricey",
                      config_json={"provider_type": "openai_compat",
                                   "api_key": "sk"},
                      is_active=True)
    session.add(conn)
    session.commit()
    session.add(ModelRole(role="writer", llm_connection_id=conn.id,
                          model_name="gpt-5"))
    for _ in range(3):
        session.add(LlmUsage(provider_label="pricey",
                             provider_type="openai_compat",
                             model="gpt-5", role="writer"))
    session.commit()

    assert api_calls_today(session, "pricey") == 3
    provider, model = await resolve_role("writer", session)
    assert provider.provider_type == "ollama"  # cap hit -> local fallback
    assert model == get_settings().writer_model


def test_ollama_usage_never_counts_toward_cap(session):
    session.add(LlmUsage(provider_label="local ollama", provider_type="ollama",
                         model="qwen", role="writer"))
    session.commit()
    assert api_calls_today(session, "local ollama") == 0


async def test_api_error_falls_back_to_local_midcall(session, monkeypatch):
    """A dying API provider must not kill the pipeline: retry locally, log it."""
    import engine.llm as llm_mod
    from engine.llm import llm_chat_text

    conn = Connection(kind=ConnectionKind.LLM, name="flaky-api",
                      config_json={"provider_type": "openai_compat",
                                   "api_key": "sk"},
                      is_active=True)
    session.add(conn)
    session.commit()
    session.add(ModelRole(role="writer", llm_connection_id=conn.id,
                          model_name="gpt-x"))
    session.commit()

    class DyingAPI(FakeProvider):
        provider_type = "openai_compat"

    class LocalFake(FakeProvider):
        provider_type = "ollama"

    dying = DyingAPI([], label="flaky-api")            # empty replies -> raises
    local = LocalFake(["local says hi"], label="local ollama")
    monkeypatch.setattr(llm_mod, "provider_from_connection", lambda c: dying)
    monkeypatch.setattr(llm_mod, "_local_ollama", lambda: local)

    result = await llm_chat_text("writer", "sys", "user")
    assert result == "local says hi"
    assert dying.calls == 1 and local.calls == 1
    rows = session.query(LlmUsage).all()
    assert len(rows) == 1 and rows[0].provider_type == "ollama"


def test_usage_summary_shape(session):
    session.add(LlmUsage(provider_label="p1", provider_type="openai_compat",
                         model="m", role="writer", tokens_in=10, tokens_out=5))
    session.add(LlmUsage(provider_label="p1", provider_type="openai_compat",
                         model="m", role="writer", tokens_in=2, tokens_out=1))
    session.commit()
    summary = usage_today_summary(session)
    assert summary["p1"]["calls"] == 2
    assert summary["p1"]["tokens_in"] == 12
    assert summary["p1"]["tokens_out"] == 6

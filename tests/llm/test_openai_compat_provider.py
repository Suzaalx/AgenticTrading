from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from sentinel.llm.providers.openai_compat import (
    PROVIDER_SPECS,
    OpenAICompatibleProvider,
    _maybe_json,
    _strip_think,
)


class SamplePayload(BaseModel):
    content: str
    confidence: int


class _UnsupportedFormat(Exception):
    status_code = 400

    def __init__(self) -> None:
        super().__init__("response_format json_schema is not supported for this model")


class FakeChatClient:
    """Minimal stand-in for openai.AsyncOpenAI: records requests, scripts responses."""

    def __init__(self, *, reply: str, reject_modes: set[str] | None = None) -> None:
        self.reply = reply
        self.reject_modes = reject_modes or set()
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        mode = (request.get("response_format") or {}).get("type", "prompt")
        if mode in self.reject_modes:
            raise _UnsupportedFormat()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        )


@pytest.mark.asyncio
async def test_json_schema_mode_is_used_when_backend_accepts_it() -> None:
    client = FakeChatClient(reply=json.dumps({"content": "ok", "confidence": 70}))
    provider = OpenAICompatibleProvider(client, name="groq")

    result = await provider.complete_structured(
        "market_analyst", "prompt", SamplePayload, model="llama-3.1-8b-instant", max_tokens=512
    )

    assert result.structured == {"content": "ok", "confidence": 70}
    assert result.input_tokens == 120 and result.output_tokens == 30
    assert result.model == "llama-3.1-8b-instant"
    assert provider.structured_mode == "json_schema"
    request = client.requests[0]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["schema"]["properties"]["confidence"]
    assert request["max_tokens"] == 512
    assert request["messages"][0]["role"] == "system"
    assert "JSON schema" in request["messages"][0]["content"]
    assert request["messages"][1] == {"role": "user", "content": "prompt"}


@pytest.mark.asyncio
async def test_falls_back_to_json_object_then_remembers_mode() -> None:
    client = FakeChatClient(
        reply='{"content": "fallback", "confidence": 1}', reject_modes={"json_schema"}
    )
    provider = OpenAICompatibleProvider(client, name="groq")

    first = await provider.complete_structured("a", "p", SamplePayload, model="m")
    second = await provider.complete_structured("a", "p", SamplePayload, model="m")

    assert first.structured["content"] == "fallback"
    assert second.structured["content"] == "fallback"
    assert provider.structured_mode == "json_object"
    # 2 attempts for the first call (json_schema rejected, json_object ok), 1 for the second.
    assert [r["response_format"]["type"] for r in client.requests] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


@pytest.mark.asyncio
async def test_falls_back_to_prompt_mode_when_no_response_format_is_supported() -> None:
    client = FakeChatClient(
        reply='Sure! ```json\n{"content": "plain", "confidence": 5}\n```',
        reject_modes={"json_schema", "json_object"},
    )
    provider = OpenAICompatibleProvider(client, name="ollama")

    result = await provider.complete_structured("a", "p", SamplePayload, model="m")

    assert result.structured == {"content": "plain", "confidence": 5}
    assert provider.structured_mode == "prompt"
    assert "response_format" not in client.requests[-1]


@pytest.mark.asyncio
async def test_non_format_errors_propagate() -> None:
    class Boom(Exception):
        status_code = 500

    async def _create(**_: Any) -> Any:
        raise Boom("upstream exploded")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    provider = OpenAICompatibleProvider(client, name="groq")

    with pytest.raises(Boom):
        await provider.complete_structured("a", "p", SamplePayload, model="m")


@pytest.mark.asyncio
async def test_think_blocks_are_stripped_before_json_extraction() -> None:
    client = FakeChatClient(
        reply='<think>reasoning about markets...</think>\n{"content": "after think", "confidence": 9}'
    )
    provider = OpenAICompatibleProvider(client, name="groq", structured_mode="prompt")

    result = await provider.complete_structured("a", "p", SamplePayload, model="qwen/qwen3-32b")

    assert result.structured == {"content": "after think", "confidence": 9}
    assert "<think>" not in result.content


@pytest.mark.asyncio
async def test_missing_key_raises_with_env_var_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = OpenAICompatibleProvider.for_name("groq")

    assert provider.client is None
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        await provider.complete_structured("a", "p", SamplePayload, model="m")


def test_for_name_resolves_specs_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    ollama = OpenAICompatibleProvider.for_name("ollama")
    gemini = OpenAICompatibleProvider.for_name("gemini")
    custom = OpenAICompatibleProvider.for_name("groq", base_url="http://proxy/v1", api_key_env="MY_KEY")

    assert ollama.base_url == "http://gpu-box:11434/v1"
    assert ollama.client is not None  # dummy key suffices for a local server
    assert gemini.base_url == PROVIDER_SPECS["gemini"].base_url
    assert gemini.client is None
    assert custom.base_url == "http://proxy/v1" and custom.api_key_env == "MY_KEY"
    with pytest.raises(ValueError, match="Unknown OpenAI-compatible provider"):
        OpenAICompatibleProvider.for_name("mistral")


def test_json_helpers() -> None:
    assert _strip_think("<think>a\nb</think> {}") == "{}"
    assert _maybe_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _maybe_json('noise before {"a": 2} noise after') == {"a": 2}
    assert _maybe_json("not json at all") is None

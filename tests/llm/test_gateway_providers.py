from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from sentinel.config.settings import LLMProviderSettings, LLMSettings, Settings, load_settings
from sentinel.llm.contracts import LLMResult
from sentinel.llm.gateway import OPEN_WEIGHT_PROVIDERS, SUPPORTED_PROVIDERS, LLMGateway
from sentinel.llm.providers.anthropic import AnthropicProvider
from sentinel.llm.providers.openai_compat import OpenAICompatibleProvider


class SamplePayload(BaseModel):
    value: int


class SlowProvider:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def complete_structured(self, **kwargs: Any) -> LLMResult[Any]:
        self.kwargs = dict(kwargs)
        await asyncio.sleep(0.02)
        return LLMResult(content='{"value": 3}', structured={"value": 3}, model="m")


@pytest.mark.parametrize("name", sorted(OPEN_WEIGHT_PROVIDERS))
def test_gateway_builds_open_weight_providers_without_keys(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    gateway = LLMGateway(settings=Settings(llm=LLMSettings(provider=name)))

    assert gateway.provider_name == name
    assert isinstance(gateway.provider, OpenAICompatibleProvider)
    assert gateway.provider.name == name


def test_gateway_default_provider_is_groq_not_anthropic() -> None:
    gateway = LLMGateway(settings=Settings())

    assert gateway.provider_name == "groq"
    assert "anthropic" in SUPPORTED_PROVIDERS


def test_gateway_keeps_anthropic_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    gateway = LLMGateway(settings=Settings(llm=LLMSettings(provider="anthropic")))

    assert isinstance(gateway.provider, AnthropicProvider)


def test_gateway_applies_provider_overrides_from_settings() -> None:
    settings = Settings(
        llm=LLMSettings(
            provider="groq",
            providers={"groq": LLMProviderSettings(base_url="http://proxy/v1", api_key_env="PROXY_KEY")},
        )
    )
    gateway = LLMGateway(settings=settings)

    assert gateway.provider.base_url == "http://proxy/v1"
    assert gateway.provider.api_key_env == "PROXY_KEY"


def test_gateway_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        LLMGateway(settings=Settings(llm=LLMSettings(provider="mystery")))


@pytest.mark.asyncio
async def test_gateway_stamps_latency_and_passes_max_tokens() -> None:
    provider = SlowProvider()
    settings = Settings(llm=LLMSettings(provider="groq", max_tokens=777, max_retries=0))
    gateway = LLMGateway(settings=settings, provider=provider)

    result = await gateway.complete_structured("tester", "prompt", SamplePayload)

    assert result.structured.value == 3
    assert result.latency_ms >= 10
    assert provider.kwargs["max_tokens"] == 777
    assert provider.kwargs["model"] == settings.llm.quick_model


def test_config_toml_defaults_to_open_weight_models() -> None:
    settings = load_settings(Path(__file__).resolve().parents[2])

    assert settings.llm.provider in OPEN_WEIGHT_PROVIDERS
    assert "claude" not in settings.llm.quick_model
    assert "claude" not in settings.llm.deep_model
    assert all("claude" not in model for model in settings.llm.role_models.values())


def test_env_file_keys_are_exported_to_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell")
    (tmp_path / ".env").write_text(
        'GROQ_API_KEY=gsk_test # inline comment\nGEMINI_API_KEY="from-file"\nEMPTY=\n',
        encoding="utf-8",
    )

    load_settings(tmp_path)

    import os

    assert os.environ["GROQ_API_KEY"] == "gsk_test"
    assert os.environ["GEMINI_API_KEY"] == "from-shell"  # real env wins over .env
    assert "EMPTY" not in os.environ


class _RateLimited(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None) -> None:
        super().__init__("rate limit exceeded")
        headers = {} if retry_after is None else {"retry-after": retry_after}
        self.response = type("Resp", (), {"headers": headers})()


class RateLimitingProvider:
    def __init__(self, failures: int, retry_after: str | None = "0.01") -> None:
        self.failures = failures
        self.retry_after = retry_after
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> LLMResult[Any]:
        self.calls += 1
        if self.calls <= self.failures:
            raise _RateLimited(self.retry_after)
        return LLMResult(content='{"value": 1}', structured={"value": 1}, model="m")


@pytest.mark.asyncio
async def test_gateway_waits_out_rate_limits_without_spending_transport_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentinel.llm.gateway as gateway_module

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gateway_module.asyncio, "sleep", fake_sleep)
    provider = RateLimitingProvider(failures=3, retry_after="7")
    gateway = LLMGateway(settings=Settings(llm=LLMSettings(provider="groq", max_retries=0)), provider=provider)

    result = await gateway.complete_structured("t", "p", SamplePayload)

    assert result.structured.value == 1
    assert provider.calls == 4
    assert sleeps == [7.0, 7.0, 7.0]  # Retry-After honoured; max_retries=0 did not apply


@pytest.mark.asyncio
async def test_gateway_gives_up_after_too_many_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.llm.gateway as gateway_module

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(gateway_module.asyncio, "sleep", fake_sleep)
    provider = RateLimitingProvider(failures=99, retry_after=None)
    gateway = LLMGateway(settings=Settings(llm=LLMSettings(provider="groq", max_retries=0)), provider=provider)

    with pytest.raises(_RateLimited):
        await gateway.complete_structured("t", "p", SamplePayload)
    assert provider.calls == gateway_module.RATE_LIMIT_MAX_RETRIES + 1

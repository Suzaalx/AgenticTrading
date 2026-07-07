from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from sentinel.config.settings import LLMSettings, Settings
from sentinel.llm.contracts import LLMResult
from sentinel.llm.gateway import LLMGateway, SchemaParseError
from sentinel.testing.fakes import FakeLLM


class SamplePayload(BaseModel):
    value: int


class SequencedProvider:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = payloads
        self.prompts: list[str] = []

    async def complete_structured(self, **kwargs: Any) -> LLMResult[Any]:
        self.prompts.append(str(kwargs["prompt"]))
        payload = self.payloads.pop(0)
        return LLMResult(content=str(payload), structured=payload, input_tokens=10, output_tokens=5, model="gpt-5.4-mini")


def _gateway(provider: Any) -> LLMGateway:
    settings = Settings(llm=LLMSettings(provider="openai", quick_model="gpt-5.4-mini", max_retries=0))
    return LLMGateway(settings=settings, provider=provider)


@pytest.mark.asyncio
async def test_gateway_retries_once_with_validation_error_feedback() -> None:
    provider = SequencedProvider([{"value": "bad"}, {"value": 7}])
    result = await _gateway(provider).complete_structured(
        agent="tester",
        prompt="original prompt",
        schema=SamplePayload,
    )

    assert result.structured.value == 7
    assert len(provider.prompts) == 2
    assert "Validation error" in provider.prompts[1]
    assert "original prompt" in provider.prompts[1]


@pytest.mark.asyncio
async def test_gateway_raises_schema_parse_error_with_raw_response_after_second_failure() -> None:
    provider = SequencedProvider([{"value": "bad"}, {"value": "still bad"}])

    with pytest.raises(SchemaParseError) as exc_info:
        await _gateway(provider).complete_structured(
            agent="tester",
            prompt="original prompt",
            schema=SamplePayload,
        )

    assert exc_info.value.raw_response == {"value": "still bad"}


@pytest.mark.asyncio
async def test_gateway_retries_malformed_json_content() -> None:
    provider = SequencedProvider([None, {"value": 9}])

    result = await _gateway(provider).complete_structured(
        agent="tester",
        prompt="original prompt",
        schema=SamplePayload,
    )

    assert result.structured.value == 9
    assert len(provider.prompts) == 2


@pytest.mark.asyncio
async def test_gateway_treats_provider_validation_error_as_schema_retry() -> None:
    attempts = 0

    def response(_agent: str, prompt: str, _schema: type[Any] | None, _kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        assert attempts == 1 or "Validation error" in prompt
        return {"value": "bad"} if attempts == 1 else {"value": 11}

    result = await _gateway(FakeLLM({"tester": response})).complete_structured(
        agent="tester",
        prompt="original prompt",
        schema=SamplePayload,
    )

    assert result.structured.value == 11
    assert attempts == 2

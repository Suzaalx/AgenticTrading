"""Provider-neutral structured LLM gateway."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from sentinel.config.settings import Settings, load_settings
from sentinel.llm.contracts import LLMResult

T = TypeVar("T")
Tier = Literal["quick", "deep"]


class SchemaParseError(RuntimeError):
    """Raised after the structured response cannot be parsed after one retry."""

    def __init__(self, message: str, *, raw_response: Any) -> None:
        super().__init__(message)
        self.raw_response = raw_response


class LLMGateway:
    """Structured-output gateway selecting quick/deep models from settings."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        provider: Any | None = None,
        provider_name: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.provider_name = provider_name or self.settings.llm.provider
        self.provider = provider or self._build_provider(self.provider_name, base_url=base_url)

    def model_for_tier(self, tier: Tier = "quick", *, agent: str | None = None) -> str:
        """Return the configured model for an agent role or gateway tier."""

        if agent is not None:
            role_model = self.settings.llm.role_models.get(agent)
            if role_model:
                return role_model
        return self.settings.llm.deep_model if tier == "deep" else self.settings.llm.quick_model

    async def complete_structured(
        self,
        agent: str,
        prompt: str,
        schema: type[T] | None = None,
        *,
        tier: Tier = "quick",
        model: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResult[T | Any]:
        """Call the selected provider and coerce the result into the requested schema."""

        selected_model = model or self.model_for_tier(tier, agent=agent)
        selected_temperature = (
            self.settings.llm.temperature if temperature is None else temperature
        )
        try:
            provider_result = await self._call_provider_with_retries(
                agent=agent,
                prompt=prompt,
                schema=schema,
                model=selected_model,
                temperature=selected_temperature,
                **kwargs,
            )
            return self._validated_result(provider_result, schema)
        except (ValidationError, ValueError, TypeError) as first_error:
            retry_prompt = (
                f"{prompt}\n\n"
                "The previous structured response failed validation. "
                "Return corrected structured output only.\n"
                f"Validation error:\n{first_error}"
            )
            retry_result: LLMResult[Any] | None = None
            try:
                retry_result = await self._call_provider_with_retries(
                    agent=agent,
                    prompt=retry_prompt,
                    schema=schema,
                    model=selected_model,
                    temperature=selected_temperature,
                    **kwargs,
                )
                return self._validated_result(retry_result, schema)
            except (ValidationError, ValueError, TypeError) as second_error:
                raise SchemaParseError(
                    f"Structured response failed validation after retry: {second_error}",
                    raw_response=self._raw_response(retry_result) if retry_result else None,
                ) from second_error

    async def _call_provider_with_retries(self, **kwargs: Any) -> LLMResult[Any]:
        max_retries = max(0, int(self.settings.llm.max_retries))
        attempt = 0
        while True:
            try:
                return await self.provider.complete_structured(**kwargs)
            except (ValidationError, ValueError, TypeError):
                raise
            except Exception:
                attempt += 1
                if attempt > max_retries:
                    raise
                await asyncio.sleep(min(0.25 * attempt, 1.0))

    def _validated_result(
        self,
        result: LLMResult[Any],
        schema: type[T] | None,
    ) -> LLMResult[T | Any]:
        if schema is None:
            return result
        structured = self._coerce(schema, result.structured, result.content)
        return LLMResult(
            content=result.content,
            structured=structured,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model=result.model,
        )

    @staticmethod
    def _coerce(schema: type[T], payload: Any, content: str) -> T:
        if isinstance(payload, schema):
            return payload
        if isinstance(payload, BaseModel):
            payload = payload.model_dump()
        if isinstance(payload, str):
            payload = _json_payload(payload)
        if payload is None:
            payload = _json_payload(content)
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_validate(payload)
        if isinstance(payload, dict):
            return schema(**payload)  # type: ignore[misc, operator]
        return schema(payload)  # type: ignore[call-arg]

    @staticmethod
    def _raw_response(result: LLMResult[Any]) -> Any:
        return result.structured if result.structured is not None else result.content

    @staticmethod
    def _build_provider(provider_name: str, *, base_url: str | None = None) -> Any:
        if provider_name == "anthropic":
            from sentinel.llm.providers.anthropic import AnthropicProvider

            return AnthropicProvider()
        if provider_name in {"openai", "openai_compatible"}:
            from sentinel.llm.providers.openai import OpenAIProvider

            resolved_base_url = base_url
            if provider_name == "openai_compatible" and resolved_base_url is None:
                resolved_base_url = os.environ.get("OPENAI_BASE_URL")
            return OpenAIProvider(base_url=resolved_base_url)
        msg = f"Unsupported LLM provider {provider_name!r}"
        raise ValueError(msg)


def _json_payload(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            return json.loads(stripped[start : end + 1])
        raise

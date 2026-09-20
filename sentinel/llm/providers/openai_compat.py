"""OpenAI-compatible structured-output adapter for free / open-weight backends.

Groq, Google Gemini (OpenAI-compatibility endpoint) and Ollama all speak the
``/chat/completions`` dialect but differ in how much of OpenAI's structured-output
surface they honour. This adapter therefore negotiates a *structured mode* per
provider instance:

1. ``json_schema`` — ``response_format={"type": "json_schema", ...}`` (best).
2. ``json_object`` — plain JSON mode with the schema embedded in the prompt.
3. ``prompt`` — no response_format at all; rely on the prompt + JSON extraction.

The first mode the backend accepts is remembered so later calls skip the fallback
dance. Reasoning models that emit ``<think>...</think>`` blocks are handled by
stripping them before JSON extraction.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from sentinel.llm.contracts import LLMResult

T = TypeVar("T")
StructuredMode = Literal["json_schema", "json_object", "prompt"]

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass(frozen=True)
class ProviderSpec:
    """Static defaults for a named OpenAI-compatible backend."""

    name: str
    base_url: str
    api_key_env: str
    # Backends that need no real key (Ollama) still require a non-empty string for the SDK.
    dummy_api_key: str | None = None
    base_url_env: str | None = None


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        base_url="http://localhost:11434/v1",
        api_key_env="OLLAMA_API_KEY",
        dummy_api_key="ollama",
        base_url_env="OLLAMA_BASE_URL",
    ),
}


class OpenAICompatibleProvider:
    """Structured-output adapter over any OpenAI-compatible chat-completions server."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        name: str = "openai_compatible",
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        dummy_api_key: str | None = None,
        structured_mode: StructuredMode | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self._structured_mode: StructuredMode | None = structured_mode
        if client is None:
            api_key = os.environ.get(api_key_env) or dummy_api_key
            if not api_key:
                self.client = None
                return
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.client = client

    @classmethod
    def for_name(
        cls,
        name: str,
        *,
        base_url: str | None = None,
        api_key_env: str | None = None,
        client: Any | None = None,
    ) -> OpenAICompatibleProvider:
        """Build a provider from the built-in spec table, honouring config/env overrides."""

        spec = PROVIDER_SPECS.get(name)
        if spec is None:
            msg = f"Unknown OpenAI-compatible provider {name!r}; known: {sorted(PROVIDER_SPECS)}"
            raise ValueError(msg)
        resolved_base_url = base_url
        if resolved_base_url is None and spec.base_url_env:
            resolved_base_url = os.environ.get(spec.base_url_env)
        return cls(
            client,
            name=spec.name,
            base_url=resolved_base_url or spec.base_url,
            api_key_env=api_key_env or spec.api_key_env,
            dummy_api_key=spec.dummy_api_key,
        )

    @property
    def structured_mode(self) -> StructuredMode | None:
        """Structured-output mode negotiated so far (None until the first call)."""

        return self._structured_mode

    async def complete_structured(
        self,
        agent: str,
        prompt: str,
        schema: type[T] | None = None,
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> LLMResult[T | Any]:
        """Call the backend and return raw text, parsed JSON payload, and token usage."""

        if self.client is None:
            msg = (
                f"{self.api_key_env} is required to start LLM-backed runs on provider "
                f"{self.name!r} (set it in .env)"
            )
            raise RuntimeError(msg)
        schema_json = _schema_json(schema) if schema is not None else None
        messages = [
            {"role": "system", "content": _system_instruction(agent, schema_json)},
            {"role": "user", "content": prompt},
        ]
        request: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
            **kwargs,
        }
        response = await self._create_with_mode_negotiation(request, schema_json)
        message = response.choices[0].message
        content = _strip_think(str(getattr(message, "content", "") or ""))
        structured: Any = _maybe_json(content) if schema is not None else content
        usage = getattr(response, "usage", None)
        return LLMResult(
            content=content,
            structured=structured,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            model=model,
        )

    async def _create_with_mode_negotiation(
        self, request: dict[str, Any], schema_json: dict[str, Any] | None
    ) -> Any:
        client = self.client
        assert client is not None  # complete_structured() raises before we get here
        if schema_json is None:
            return await client.chat.completions.create(**request)
        modes: list[StructuredMode] = (
            [self._structured_mode]
            if self._structured_mode is not None
            else ["json_schema", "json_object", "prompt"]
        )
        last_error: Exception | None = None
        for mode in modes:
            attempt = dict(request)
            response_format = _response_format(mode, schema_json)
            if response_format is not None:
                attempt["response_format"] = response_format
            try:
                response = await client.chat.completions.create(**attempt)
            except Exception as exc:  # provider SDK error types vary
                if mode == modes[-1] or not _is_unsupported_format_error(exc):
                    raise
                last_error = exc
                continue
            self._structured_mode = mode
            return response
        assert last_error is not None
        raise last_error


def _response_format(mode: StructuredMode, schema_json: dict[str, Any]) -> dict[str, Any] | None:
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": "emit_structured_report", "schema": schema_json},
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _is_unsupported_format_error(exc: Exception) -> bool:
    """Heuristically detect 'response_format not supported' 4xx errors from the backend."""

    status = getattr(exc, "status_code", None)
    if status is not None and int(status) not in {400, 404, 415, 422}:
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("response_format", "json_schema", "json_object", "structured", "not supported", "invalid")
    )


def _system_instruction(agent: str, schema_json: dict[str, Any] | None) -> str:
    base = f"You are the {agent} for a paper-trading research pipeline."
    if schema_json is None:
        return base
    return (
        f"{base} Respond with a single JSON object that conforms to the JSON schema below. "
        "Output JSON only: no prose, no markdown fences, no explanations outside the object.\n\n"
        f"JSON schema:\n{json.dumps(schema_json, separators=(',', ':'))}"
    )


def _schema_json(schema: type[Any]) -> dict[str, Any]:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return {"type": "object"}


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _maybe_json(text: str) -> Any:
    """Best-effort JSON extraction; None lets the gateway retry from ``content``."""

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
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

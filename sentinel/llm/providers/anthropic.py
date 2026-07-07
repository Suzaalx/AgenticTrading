"""Anthropic structured-output provider adapter."""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

from pydantic import BaseModel

from sentinel.llm.contracts import LLMResult

T = TypeVar("T")


class AnthropicProvider:
    """Thin async wrapper around Anthropic Messages tool-use structured output."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                self.client = None
                return
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic()
        self.client = client

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
        """Call Anthropic and return raw text, parsed payload, and token usage."""

        if self.client is None:
            msg = "ANTHROPIC_API_KEY is required to start LLM-backed runs"
            raise RuntimeError(msg)
        tools = None
        tool_choice = None
        if schema is not None:
            tools = [
                {
                    "name": "emit_structured_report",
                    "description": f"Emit the structured report for {agent}.",
                    "input_schema": _schema_json(schema),
                }
            ]
            tool_choice = {"type": "tool", "name": "emit_structured_report"}
        request: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            **kwargs,
        }
        if tools is not None:
            request["tools"] = tools
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        response = await self.client.messages.create(**request)
        text_parts: list[str] = []
        structured: Any = None
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(str(getattr(block, "text", "")))
            elif block_type == "tool_use":
                structured = getattr(block, "input", None)
        content = "\n".join(part for part in text_parts if part).strip()
        if structured is None and content:
            structured = _maybe_json(content)
        usage = getattr(response, "usage", None)
        return LLMResult(
            content=content or json.dumps(structured, default=str),
            structured=structured,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=model,
        )


def _schema_json(schema: type[Any]) -> dict[str, Any]:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return {"type": "object"}


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

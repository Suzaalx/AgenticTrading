"""OpenAI and OpenAI-compatible structured-output provider adapter."""

from __future__ import annotations

import os
from typing import Any, TypeVar

from sentinel.llm.contracts import LLMResult

T = TypeVar("T")


class OpenAIProvider:
    """Thin async wrapper around OpenAI structured output parsing."""

    def __init__(self, client: Any | None = None, *, base_url: str | None = None) -> None:
        if client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                self.client = None
                return
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=base_url)
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
        """Call OpenAI and return raw text, parsed payload, and token usage."""

        if self.client is None:
            msg = "OPENAI_API_KEY is required to start LLM-backed runs"
            raise RuntimeError(msg)
        if schema is not None:
            response = await self.client.beta.chat.completions.parse(
                model=model,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
                response_format=schema,
                max_tokens=max_tokens,
                **kwargs,
            )
            message = response.choices[0].message
            content = getattr(message, "content", "") or ""
            structured = getattr(message, "parsed", None)
        else:
            response = await self.client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                **kwargs,
            )
            message = response.choices[0].message
            content = getattr(message, "content", "") or ""
            structured = content
        usage = getattr(response, "usage", None)
        return LLMResult(
            content=content,
            structured=structured,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            model=model,
        )

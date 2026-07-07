"""Dependency-light LLM gateway contracts shared by llm/ and tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class LLMResult[T]:
    """Structured-output result returned by the future LLM gateway."""

    content: str
    structured: T
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "fake"


class StructuredLLM(Protocol):
    """Protocol for providers that can return structured output."""

    async def complete_structured(
        self,
        agent: str,
        prompt: str,
        schema: type[T] | None = None,
        **kwargs: Any,
    ) -> LLMResult[T | Any]: ...

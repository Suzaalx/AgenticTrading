"""Testing fakes for downstream agents and orchestrator integration tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from sentinel.llm.contracts import LLMResult

T = TypeVar("T")
ResponseFactory = Callable[[str, str, type[Any] | None, dict[str, Any]], Any]


@dataclass(frozen=True)
class FakeLLMCall:
    """Recorded FakeLLM invocation."""

    agent: str
    prompt: str
    schema: type[Any] | None
    kwargs: dict[str, Any]


class FakeLLM:
    """Canned structured-output harness that never touches the network.

    Map agent names to payloads or callables. Payloads may be pydantic models, dictionaries
    accepted by the requested schema, or already-built LLMResult objects. Calls are recorded
    for assertions by downstream orchestrator and agent tests.
    """

    def __init__(self, responses: dict[str, Any | ResponseFactory], model: str = "fake-model") -> None:
        self.responses = responses
        self.model = model
        self.calls: list[FakeLLMCall] = []

    async def complete_structured(
        self,
        agent: str,
        prompt: str,
        schema: type[T] | None = None,
        **kwargs: Any,
    ) -> LLMResult[T | Any]:
        """Return a canned result for an agent, coercing through schema when supplied."""

        self.calls.append(FakeLLMCall(agent=agent, prompt=prompt, schema=schema, kwargs=dict(kwargs)))
        if agent not in self.responses:
            raise KeyError(f"No FakeLLM response configured for agent {agent!r}")
        payload_or_factory = self.responses[agent]
        payload = (
            payload_or_factory(agent, prompt, schema, kwargs)
            if callable(payload_or_factory)
            else payload_or_factory
        )
        if isinstance(payload, LLMResult):
            return payload
        structured: Any = payload
        if schema is not None and isinstance(payload, dict):
            structured = schema(**payload)
        return LLMResult(
            content=getattr(structured, "content", str(structured)),
            structured=structured,
            input_tokens=0,
            output_tokens=0,
            model=self.model,
        )

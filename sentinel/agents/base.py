"""Reusable async base class for Sentinel LLM agents."""

from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from string import Formatter
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from sentinel.config.settings import Settings, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentCompleted, AgentStarted, CostIncurred, utc_now
from sentinel.core.models import AgentReport, RunState
from sentinel.llm.budget import assert_within_budget
from sentinel.llm.contracts import StructuredLLM
from sentinel.llm.cost import cost_usd, record_cost
from sentinel.store.db import connect, run_migrations

Tier = Literal["quick", "deep"]


class AgentPayload(BaseModel):
    """Base schema for LLM-generated agent payloads before the envelope is filled."""

    content: str


class _SafeMapping(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Agent(ABC):
    """Template-method base for LLM agents.

    Subclasses define class attributes for identity, tier, prompt template, response schema,
    and final report type, then implement build_template_values().
    """

    agent_name: ClassVar[str]
    tier: ClassVar[Tier] = "quick"
    prompt_template: ClassVar[str]
    response_schema: ClassVar[type[BaseModel]]
    report_type: ClassVar[type[AgentReport]]

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.bus = bus
        self.settings = settings or load_settings()
        self.conn = conn or connect()
        if conn is None:
            run_migrations(self.conn)

    async def run(self, state: RunState) -> AgentReport:
        """Run the agent and return a fully enveloped typed report."""

        assert_within_budget(self.conn, Decimal(str(self.settings.llm.monthly_budget_usd)))
        model = self._model_for_start()
        await self.bus.publish(AgentStarted(run_id=state.run_id, agent=self.agent_name, model=model))
        started = time.perf_counter()
        prompt = self.build_prompt(state)
        result = await self.llm.complete_structured(
            agent=self.agent_name,
            prompt=prompt,
            schema=self.response_schema,
            tier=self.tier,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        payload = self._payload_data(result.structured)
        content = str(payload.get("content") or result.content)
        incurred = cost_usd(result.model, result.input_tokens, result.output_tokens)
        record_cost(
            self.conn,
            run_id=state.run_id,
            agent=self.agent_name,
            model=result.model,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            cost=incurred,
        )
        await self.bus.publish(
            CostIncurred(
                run_id=state.run_id,
                agent=self.agent_name,
                model=result.model,
                tokens_in=result.input_tokens,
                tokens_out=result.output_tokens,
                cost_usd=incurred,
            )
        )
        report_data = {
            **payload,
            "run_id": state.run_id,
            "agent": self.agent_name,
            "model": result.model,
            "created_at": utc_now(),
            "latency_ms": latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": incurred,
            "content": content,
        }
        report = self.report_type.model_validate(report_data)
        await self.bus.publish(AgentCompleted(run_id=state.run_id, report=report))
        return report

    def build_prompt(self, state: RunState) -> str:
        """Load the prompt template and substitute subclass-provided values."""

        template_path = Path(__file__).with_name("prompts") / self.prompt_template
        template = template_path.read_text(encoding="utf-8")
        values = _SafeMapping(self.build_template_values(state))
        return Formatter().vformat(template, (), values)

    @abstractmethod
    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        """Return prompt-template placeholder values for the current run state."""

    def _model_for_start(self) -> str:
        model_for_tier = getattr(self.llm, "model_for_tier", None)
        if callable(model_for_tier):
            return str(model_for_tier(self.tier))
        return str(getattr(self.llm, "model", "unknown"))

    @staticmethod
    def _payload_data(payload: Any) -> dict[str, Any]:
        if isinstance(payload, BaseModel):
            return payload.model_dump()
        if isinstance(payload, dict):
            return dict(payload)
        msg = f"Unsupported agent payload type {type(payload)!r}"
        raise TypeError(msg)

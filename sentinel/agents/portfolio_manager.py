"""Portfolio Manager advisory gate."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from sentinel.agents.base import Agent, AgentPayload
from sentinel.agents.researchers import render_lessons, render_transcript
from sentinel.agents.trader import (
    _lessons_from_state,
    _portfolio_from_state,
    render_option_risk_context,
    render_portfolio,
)
from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.models import Lesson, PMDecision, Portfolio, RunState
from sentinel.llm.contracts import StructuredLLM


class PortfolioManager(Agent):
    """Deep-tier final LLM advisory decision before deterministic risk gates."""

    class Payload(AgentPayload):
        verdict: Literal["APPROVE", "REVISE", "REJECT"]
        approved_quantity_pct: float = Field(ge=0, le=100)
        reasoning: str
        lessons_applied: list[str]

    agent_name: ClassVar[str] = "portfolio_manager"
    tier: ClassVar[Literal["deep"]] = "deep"
    prompt_template: ClassVar[str] = "portfolio_manager.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[PMDecision]] = PMDecision

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
        portfolio: Portfolio | None = None,
        lessons: list[Lesson] | None = None,
    ) -> None:
        super().__init__(llm=llm, bus=bus, conn=conn, settings=settings)
        self.portfolio = portfolio
        self.lessons = lessons or []

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        if state.trade_proposal is None and state.option_proposal is None:
            msg = "PortfolioManager requires RunState.trade_proposal or RunState.option_proposal"
            raise ValueError(msg)
        portfolio = _portfolio_from_state(state) or self.portfolio
        proposal = state.option_proposal or state.trade_proposal
        return {
            "run_id": state.run_id,
            "symbol": state.symbol,
            "as_of": state.as_of.isoformat(),
            "investment_plan": _model_json_block(state.investment_plan),
            "trade_proposal": _model_json_block(proposal),
            "option_risk_context": render_option_risk_context(state, portfolio),
            "portfolio_summary": render_portfolio(portfolio),
            "risk_transcript": render_transcript(state.risk_transcript),
            "lessons": render_lessons([*_lessons_from_state(state), *self.lessons]),
        }


def _model_json_block(model: Any) -> str:
    if model is None:
        return "_Not provided._"
    if hasattr(model, "model_dump_json"):
        return "```json\n" + model.model_dump_json(indent=2) + "\n```"
    return str(model)

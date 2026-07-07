"""Trader agent that converts an investment plan into a trade proposal."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import Field

from sentinel.agents.base import Agent, AgentPayload
from sentinel.agents.researchers import render_lessons
from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.models import Lesson, Portfolio, Position, RunState, TradeProposal
from sentinel.llm.contracts import StructuredLLM


class Trader(Agent):
    """Deep-tier execution planner."""

    class Payload(AgentPayload):
        action: Literal["BUY", "SELL", "HOLD"]
        quantity_pct: float = Field(ge=0, le=100)
        order_type: Literal["market"]
        time_horizon_days: int
        entry_rationale: str
        exit_plan: str
        stop_loss_pct: float | None
        take_profit_pct: float | None

    agent_name: ClassVar[str] = "trader"
    tier: ClassVar[Literal["deep"]] = "deep"
    prompt_template: ClassVar[str] = "trader.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[TradeProposal]] = TradeProposal

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
        if state.investment_plan is None:
            msg = "Trader requires RunState.investment_plan"
            raise ValueError(msg)
        portfolio = _portfolio_from_state(state) or self.portfolio
        return {
            "run_id": state.run_id,
            "symbol": state.symbol,
            "as_of": state.as_of.isoformat(),
            "investment_plan": _model_json_block(state.investment_plan),
            "current_position": render_position(_position_for_symbol(portfolio, state.symbol)),
            "portfolio_summary": render_portfolio(portfolio),
            "lessons": render_lessons([*_lessons_from_state(state), *self.lessons]),
        }


def render_portfolio(portfolio: Portfolio | None) -> str:
    if portfolio is None:
        return "_No portfolio state provided._"
    rows = [
        ("cash", str(portfolio.cash)),
        ("equity_at_cost", str(portfolio.equity)),
        ("day_pnl", str(portfolio.day_pnl)),
        ("open_positions", str(len(portfolio.positions))),
    ]
    exposure = sum((position.cost_basis for position in portfolio.positions), Decimal("0"))
    rows.append(("gross_exposure_at_cost", str(exposure)))
    body = ["| field | value |", "| --- | --- |"]
    body.extend(f"| {field} | {value} |" for field, value in rows)
    if portfolio.positions:
        body.extend(["", "#### Positions", "| symbol | qty | avg_cost | cost_basis |", "| --- | --- | --- | --- |"])
        body.extend(
            f"| {p.symbol} | {p.qty} | {p.avg_cost} | {p.cost_basis} |"
            for p in portfolio.positions
        )
    return "\n".join(body)


def render_position(position: Position | None) -> str:
    if position is None:
        return "_No current position in this symbol._"
    return "\n".join(
        [
            "| field | value |",
            "| --- | --- |",
            f"| symbol | {position.symbol} |",
            f"| qty | {position.qty} |",
            f"| avg_cost | {position.avg_cost} |",
            f"| cost_basis | {position.cost_basis} |",
            f"| stop_loss_pct | {position.stop_loss_pct} |",
            f"| take_profit_pct | {position.take_profit_pct} |",
            f"| opened_at | {position.opened_at.isoformat()} |",
            f"| horizon_days | {position.horizon_days} |",
        ]
    )


def _position_for_symbol(portfolio: Portfolio | None, symbol: str) -> Position | None:
    if portfolio is None:
        return None
    return next((position for position in portfolio.positions if position.symbol == symbol), None)


def _portfolio_from_state(state: RunState) -> Portfolio | None:
    raw = getattr(state, "portfolio", None)
    if raw is None:
        return None
    return raw if isinstance(raw, Portfolio) else Portfolio.model_validate(raw)


def _lessons_from_state(state: RunState) -> list[Lesson]:
    raw = getattr(state, "lessons", None)
    if raw is None:
        template_values = getattr(state, "template_values", None)
        if isinstance(template_values, Mapping):
            raw = template_values.get("lessons")
    if raw is None:
        return []
    return [lesson if isinstance(lesson, Lesson) else Lesson.model_validate(lesson) for lesson in raw]


def _model_json_block(model: Any) -> str:
    if hasattr(model, "model_dump_json"):
        return "```json\n" + model.model_dump_json(indent=2) + "\n```"
    return str(model)

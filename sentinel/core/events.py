"""Typed events emitted on Sentinel's in-process event bus."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from sentinel.core.models import AgentReport, DebateTurn, Fill, GateResult, Order, PMDecision, Quote


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class Event(BaseModel):
    """Base event with a creation timestamp."""

    ts: datetime = Field(default_factory=utc_now)


class RunStarted(Event):
    run_id: str
    symbol: str
    as_of: datetime
    mode: Literal["decision", "backtest_step"] = "decision"


class StageChanged(Event):
    run_id: str
    stage: str
    status: str


class AgentStarted(Event):
    run_id: str
    agent: str
    model: str


class AgentCompleted(Event):
    run_id: str
    report: AgentReport


class DebateTurnAdded(Event):
    run_id: str
    turn: DebateTurn
    debate: Literal["research", "risk"] = "research"


class DecisionMade(Event):
    run_id: str
    decision: PMDecision


class GateEvaluated(Event):
    run_id: str
    result: GateResult


class OrderSubmitted(Event):
    order: Order


class OrderFilled(Event):
    order_id: str
    fill: Fill


class QuoteTick(Event):
    quote: Quote


class EquityUpdated(Event):
    equity: Decimal
    cash: Decimal
    day_pnl: Decimal


class ReconciliationFailed(Event):
    venue: str
    diff: dict[str, Any] = Field(default_factory=dict)


class KillSwitchChanged(Event):
    enabled: bool
    actor: str = "system"


class KillEngaged(Event):
    actor: str = "system"
    cancel_results: dict[str, Any] = Field(default_factory=dict)
    flatten_results: dict[str, Any] = Field(default_factory=dict)


class CostIncurred(Event):
    run_id: str | None
    agent: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


class LogLine(Event):
    level: Literal["debug", "info", "warning", "error", "critical"]
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class BacktestProgress(Event):
    bt_id: str
    completed_steps: int
    total_steps: int
    message: str

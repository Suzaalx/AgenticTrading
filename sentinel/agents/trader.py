"""Trader agent that converts an investment plan into a trade proposal."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import Field, ValidationError, model_validator

from sentinel.agents.base import Agent, AgentPayload
from sentinel.agents.researchers import render_lessons
from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentCompleted, AgentStarted, CostIncurred, utc_now
from sentinel.core.models import (
    Lesson,
    OptionStrategyCandidate,
    OptionStrategyProposal,
    Portfolio,
    Position,
    RunState,
    TradeProposal,
)
from sentinel.llm.budget import assert_within_budget
from sentinel.llm.contracts import StructuredLLM
from sentinel.llm.cost import cost_usd, record_cost
from sentinel.llm.gateway import SchemaParseError


class Trader(Agent):
    """Deep-tier execution planner."""

    class Payload(AgentPayload):
        action: Literal["BUY", "SELL", "HOLD", "OPEN", "CLOSE"]
        quantity_pct: float | None = Field(default=None, ge=0, le=100)
        order_type: Literal["market"] | None = None
        strategy: str | None = None
        legs: list[Any] | None = None
        candidate_id: str | None = None
        max_loss_usd: Decimal | None = None
        time_horizon_days: int
        entry_rationale: str
        exit_plan: str
        stop_loss_pct: float | None = None
        take_profit_pct: float | None = None
        stop_loss_pct_premium: float | None = None
        take_profit_pct_premium: float | None = None

        @model_validator(mode="after")
        def _validate_union(self) -> Trader.Payload:
            if _payload_is_option(self):
                missing = [name for name in ("strategy", "legs", "candidate_id", "max_loss_usd") if getattr(self, name) is None]
                if missing:
                    msg = f"OptionStrategyProposal payload missing required fields: {', '.join(missing)}"
                    raise ValueError(msg)
                if self.action not in {"OPEN", "CLOSE", "HOLD"}:
                    msg = "OptionStrategyProposal action must be OPEN, CLOSE, or HOLD"
                    raise ValueError(msg)
            else:
                missing = [name for name in ("quantity_pct", "order_type") if getattr(self, name) is None]
                if missing:
                    msg = f"TradeProposal payload missing required fields: {', '.join(missing)}"
                    raise ValueError(msg)
                if self.action not in {"BUY", "SELL", "HOLD"}:
                    msg = "TradeProposal action must be BUY, SELL, or HOLD"
                    raise ValueError(msg)
            return self

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
            "option_candidates": render_option_candidates(state.option_candidates),
            "lessons": render_lessons([*_lessons_from_state(state), *self.lessons]),
        }

    async def run(self, state: RunState) -> TradeProposal | OptionStrategyProposal:
        """Run trader and validate option candidate references against offered candidates."""

        if state.investment_plan is None:
            msg = "Trader requires RunState.investment_plan"
            raise ValueError(msg)
        assert_within_budget(self.conn, Decimal(str(self.settings.llm.monthly_budget_usd)))
        model = self._model_for_start()
        await self.bus.publish(AgentStarted(run_id=state.run_id, agent=self.agent_name, model=model))
        started = time.perf_counter()
        prompt = self.build_prompt(state)
        first_error: Exception | None = None
        result = None
        for attempt in range(2):
            try:
                result = await self.llm.complete_structured(
                    agent=self.agent_name,
                    prompt=prompt,
                    schema=self.response_schema,
                    tier=self.tier,
                )
                payload = self._payload_data(result.structured)
                report = self._proposal_from_payload(state, payload, result.content, result.model, started, result.input_tokens, result.output_tokens)
                incurred = report.cost_usd
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
                if isinstance(report, OptionStrategyProposal):
                    state.option_proposal = report
                else:
                    state.trade_proposal = report
                await self.bus.publish(AgentCompleted(run_id=state.run_id, report=report))
                return report
            except (ValidationError, ValueError, TypeError) as exc:
                first_error = exc
                if attempt == 1:
                    break
                prompt = (
                    f"{prompt}\n\n"
                    "The previous structured response failed validation. "
                    "Return corrected structured output only.\n"
                    f"Validation error:\n{exc}"
                )
        raw = result.structured if result is not None else None
        raise SchemaParseError(
            f"Trader structured response failed validation after retry: {first_error}",
            raw_response=raw,
        ) from first_error

    def _proposal_from_payload(
        self,
        state: RunState,
        payload: dict[str, Any],
        fallback_content: str,
        model: str,
        started: float,
        input_tokens: int,
        output_tokens: int,
    ) -> TradeProposal | OptionStrategyProposal:
        content = str(payload.get("content") or fallback_content)
        report_data = {
            **payload,
            "run_id": state.run_id,
            "agent": self.agent_name,
            "model": model,
            "created_at": utc_now(),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd(model, input_tokens, output_tokens),
            "content": content,
        }
        if _payload_is_option(payload):
            candidate_id = str(payload.get("candidate_id"))
            candidates = _candidate_map(state.option_candidates)
            if candidate_id not in candidates:
                msg = f"candidate_id {candidate_id!r} was not offered; valid ids: {', '.join(candidates) or 'none'}"
                raise ValueError(msg)
            candidate = candidates[candidate_id]
            report_data["strategy"] = candidate.strategy
            report_data["legs"] = candidate.legs
            report_data["max_loss_usd"] = candidate.max_loss
            return OptionStrategyProposal.model_validate(report_data)
        return TradeProposal.model_validate(report_data)


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


def render_option_candidates(candidates: list[OptionStrategyCandidate]) -> str:
    if not candidates:
        return "_No option strategy candidates were offered._"
    lines = [
        "| candidate_id | strategy | max_loss | max_gain | net_premium | breakevens | est_pop | net_delta | net_vega | net_theta | liquidity_score | rationale_facts | legs |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for candidate_id, candidate in _candidate_map(candidates).items():
        lines.append(
            "| "
            + " | ".join(
                [
                    candidate_id,
                    candidate.strategy,
                    str(candidate.max_loss),
                    str(candidate.max_gain),
                    str(candidate.net_premium),
                    ", ".join(str(value) for value in candidate.breakevens) or "none",
                    "unknown" if candidate.est_pop is None else f"{candidate.est_pop:.4f}",
                    f"{candidate.net_delta:.4f}",
                    f"{candidate.net_vega:.4f}",
                    f"{candidate.net_theta:.4f}",
                    f"{candidate.liquidity_score:.4f}",
                    candidate.rationale_facts.replace("|", "\\|"),
                    "; ".join(_render_leg(leg) for leg in candidate.legs),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_option_risk_context(state: RunState, portfolio: Portfolio | None) -> str:
    proposal = state.option_proposal
    if proposal is None:
        return "_No option proposal selected._"
    candidate = _candidate_map(state.option_candidates).get(proposal.candidate_id)
    equity = portfolio.equity if portfolio is not None else None
    max_loss_pct = (
        (proposal.max_loss_usd / equity * Decimal("100")) if equity is not None and equity > 0 else None
    )
    rows = [
        ("candidate_id", proposal.candidate_id),
        ("strategy", proposal.strategy),
        ("action", proposal.action),
        ("max_loss_usd", proposal.max_loss_usd),
        ("max_loss_pct_equity", max_loss_pct),
        ("collateral_proxy_usd", proposal.max_loss_usd),
        ("net_delta", candidate.net_delta if candidate else None),
        ("net_vega", candidate.net_vega if candidate else None),
        ("net_theta", candidate.net_theta if candidate else None),
        ("liquidity_score", candidate.liquidity_score if candidate else None),
        ("greeks_headroom_note", "WS7 gate re-checks mandate delta/vega caps against live portfolio."),
    ]
    body = ["| field | value |", "| --- | --- |"]
    body.extend(f"| {field} | {value} |" for field, value in rows)
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


def _payload_is_option(payload: Any) -> bool:
    if isinstance(payload, Mapping):
        return str(payload.get("action")) in {"OPEN", "CLOSE"} or any(
            payload.get(name) is not None for name in ("strategy", "candidate_id", "max_loss_usd")
        )
    return str(getattr(payload, "action", "")) in {"OPEN", "CLOSE"} or any(
        getattr(payload, name, None) is not None for name in ("strategy", "candidate_id", "max_loss_usd")
    )


def _candidate_map(candidates: list[OptionStrategyCandidate]) -> dict[str, OptionStrategyCandidate]:
    return {f"candidate_{index}": candidate for index, candidate in enumerate(candidates, start=1)}


def _render_leg(leg: Any) -> str:
    contract = leg.contract
    return (
        f"{leg.side} {leg.contracts} {contract.contract_symbol} "
        f"{contract.expiry.isoformat()} {contract.kind} {contract.strike} @ {leg.limit_price}"
    )

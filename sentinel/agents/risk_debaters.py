"""Risk debate agents and helper."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from string import Formatter
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from sentinel.config.settings import Settings, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentStarted, CostIncurred, DebateTurnAdded
from sentinel.core.models import DebateTurn, Portfolio, RunState
from sentinel.llm.budget import assert_within_budget
from sentinel.llm.contracts import StructuredLLM
from sentinel.llm.cost import cost_usd, record_cost
from sentinel.store.db import connect, run_migrations

from .researchers import render_transcript
from .trader import render_option_risk_context, render_portfolio

_MAX_TURN_WORDS = 350


class RiskPayload(BaseModel):
    argument: str


class _SafeMapping(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class RiskDebater:
    """Base for aggressive/conservative/neutral risk perspectives."""

    agent_name: ClassVar[str]
    perspective: ClassVar[str]
    speaker: ClassVar[Literal["bull", "bear"]]
    prompt_template: ClassVar[str]
    tier: ClassVar[Literal["quick"]] = "quick"

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
        portfolio: Portfolio | None = None,
    ) -> None:
        self.llm = llm
        self.bus = bus
        self.settings = settings or load_settings()
        self.conn = conn or connect()
        if conn is None:
            run_migrations(self.conn)
        self.portfolio = portfolio

    async def run(self, state: RunState, *, round_number: int) -> DebateTurn:
        """Generate one capped risk debate turn."""

        if state.trade_proposal is None and state.option_proposal is None:
            msg = "Risk debate requires RunState.trade_proposal or RunState.option_proposal"
            raise ValueError(msg)
        assert_within_budget(self.conn, Decimal(str(self.settings.llm.monthly_budget_usd)))
        model = self._model_for_start()
        await self.bus.publish(AgentStarted(run_id=state.run_id, agent=self.agent_name, model=model))
        prompt = self.build_prompt(state, round_number=round_number)
        result = await self.llm.complete_structured(
            agent=self.agent_name,
            prompt=prompt,
            schema=RiskPayload,
            tier=self.tier,
        )
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
        argument = _payload_text(result.structured, fallback=result.content)
        prefix = f"[{self.perspective}] "
        if not argument.lower().startswith(prefix.lower()):
            argument = prefix + argument
        return DebateTurn(
            speaker=self.speaker,
            round=round_number,
            argument=_truncate_words(argument),
        )

    def build_prompt(self, state: RunState, *, round_number: int) -> str:
        portfolio = _portfolio_from_state(state) or self.portfolio
        proposal = state.option_proposal or state.trade_proposal
        template_path = Path(__file__).with_name("prompts") / self.prompt_template
        template = template_path.read_text(encoding="utf-8")
        values = _SafeMapping(
            {
                "run_id": state.run_id,
                "symbol": state.symbol,
                "as_of": state.as_of.isoformat(),
                "round_number": str(round_number),
                "investment_plan": _model_json_block(state.investment_plan),
                "trade_proposal": _model_json_block(proposal),
                "option_risk_context": render_option_risk_context(state, portfolio),
                "portfolio_summary": render_portfolio(portfolio),
                "risk_transcript": render_transcript(state.risk_transcript),
            }
        )
        return Formatter().vformat(template, (), values)

    def _model_for_start(self) -> str:
        model_for_tier = getattr(self.llm, "model_for_tier", None)
        if callable(model_for_tier):
            try:
                return str(model_for_tier(self.tier, agent=self.agent_name))
            except TypeError:
                return str(model_for_tier(self.tier))
        return str(getattr(self.llm, "model", "unknown"))


class AggressiveRisk(RiskDebater):
    """Upside/urgency risk perspective."""

    agent_name: ClassVar[str] = "aggressive_risk"
    perspective: ClassVar[str] = "aggressive"
    speaker: ClassVar[Literal["bull"]] = "bull"
    prompt_template: ClassVar[str] = "aggressive_risk.md"


class ConservativeRisk(RiskDebater):
    """Capital-preservation risk perspective."""

    agent_name: ClassVar[str] = "conservative_risk"
    perspective: ClassVar[str] = "conservative"
    speaker: ClassVar[Literal["bear"]] = "bear"
    prompt_template: ClassVar[str] = "conservative_risk.md"


class NeutralRisk(RiskDebater):
    """Arbitrating risk perspective."""

    agent_name: ClassVar[str] = "neutral_risk"
    perspective: ClassVar[str] = "neutral"
    speaker: ClassVar[Literal["bull"]] = "bull"
    prompt_template: ClassVar[str] = "neutral_risk.md"


async def run_risk_debate(
    state: RunState,
    aggressive: AggressiveRisk,
    conservative: ConservativeRisk,
    neutral: NeutralRisk,
    *,
    max_rounds: int | None = None,
) -> list[DebateTurn]:
    """Run aggressive→conservative→neutral risk debate, append turns, and emit events."""

    rounds = (
        max_rounds
        if max_rounds is not None
        else aggressive.settings.pipeline.max_risk_discuss_rounds
    )
    for round_number in range(1, max(0, rounds) + 1):
        for agent in (aggressive, conservative, neutral):
            turn = await agent.run(state, round_number=round_number)
            state.risk_transcript.append(turn)
            await agent.bus.publish(DebateTurnAdded(run_id=state.run_id, turn=turn, debate="risk"))
    return state.risk_transcript


def _payload_text(payload: Any, *, fallback: str) -> str:
    if isinstance(payload, RiskPayload):
        return payload.argument
    if isinstance(payload, dict):
        return str(payload.get("argument") or payload.get("content") or fallback)
    return str(getattr(payload, "argument", fallback))


def _portfolio_from_state(state: RunState) -> Portfolio | None:
    raw = getattr(state, "portfolio", None)
    if raw is None:
        return None
    return raw if isinstance(raw, Portfolio) else Portfolio.model_validate(raw)


def _model_json_block(model: Any) -> str:
    if model is None:
        return "_Not provided._"
    if hasattr(model, "model_dump_json"):
        return "```json\n" + model.model_dump_json(indent=2) + "\n```"
    return str(model)


def _truncate_words(text: str, *, max_words: int = _MAX_TURN_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " …"

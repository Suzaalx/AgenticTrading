"""Analyst agents implemented in Phase 2."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import Field

from sentinel.agents.base import Agent, AgentPayload
from sentinel.core.events import AgentCompleted, AgentStarted, utc_now
from sentinel.core.models import (
    FundamentalsAnalystReport,
    FundamentalsSnapshot,
    MarketAnalystReport,
    NewsAnalystReport,
    NewsItem,
    OptionsAnalystReport,
    RunState,
    SentimentAnalystReport,
)


class MarketAnalyst(Agent):
    """Technical analyst using the last 90 bars plus indicator values."""

    class Payload(AgentPayload):
        trend: Literal["strong_up", "up", "sideways", "down", "strong_down"]
        support: float | None
        resistance: float | None
        signals: list[str]
        confidence: int = Field(ge=0, le=100)

    agent_name: ClassVar[str] = "market_analyst"
    tier: ClassVar[Literal["quick"]] = "quick"
    prompt_template: ClassVar[str] = "market_analyst.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[MarketAnalystReport]] = MarketAnalystReport

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        if state.snapshot is None:
            msg = "MarketAnalyst requires RunState.snapshot"
            raise ValueError(msg)
        return {
            "run_id": state.run_id,
            "symbol": state.snapshot.symbol,
            "as_of": state.snapshot.as_of.isoformat(),
            "ohlcv_table": _render_frame(state.snapshot.ohlcv_path, max_rows=self._history_bars()),
            "indicator_table": _render_frame(state.snapshot.indicators_path, max_rows=self._history_bars()),
        }


    def _history_bars(self) -> int:
        return max(1, int(getattr(self.settings.pipeline, "analyst_history_bars", 90)))


class FundamentalsAnalyst(Agent):
    """Fundamental analyst using the point-in-time fundamentals snapshot."""

    class Payload(AgentPayload):
        valuation: Literal["cheap", "fair", "expensive", "unknown"]
        quality_flags: list[str]
        red_flags: list[str]
        upcoming_catalysts: list[str]
        confidence: int = Field(ge=0, le=100)

    agent_name: ClassVar[str] = "fundamentals_analyst"
    tier: ClassVar[Literal["quick"]] = "quick"
    prompt_template: ClassVar[str] = "fundamentals_analyst.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[FundamentalsAnalystReport]] = FundamentalsAnalystReport

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        if state.snapshot is None:
            msg = "FundamentalsAnalyst requires RunState.snapshot"
            raise ValueError(msg)
        return {
            "run_id": state.run_id,
            "symbol": state.snapshot.symbol,
            "as_of": state.snapshot.as_of.isoformat(),
            "fundamentals_table": _render_fundamentals(state.snapshot.fundamentals),
        }


class NewsAnalyst(Agent):
    """News analyst using recent headlines and summaries."""

    class Payload(AgentPayload):
        sentiment: Literal["very_negative", "negative", "neutral", "positive", "very_positive"]
        key_events: list[str]
        macro_context: str
        confidence: int = Field(ge=0, le=100)

    agent_name: ClassVar[str] = "news_analyst"
    tier: ClassVar[Literal["quick"]] = "quick"
    prompt_template: ClassVar[str] = "news_analyst.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[NewsAnalystReport]] = NewsAnalystReport

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        if state.snapshot is None:
            msg = "NewsAnalyst requires RunState.snapshot"
            raise ValueError(msg)
        return {
            "run_id": state.run_id,
            "symbol": state.snapshot.symbol,
            "as_of": state.snapshot.as_of.isoformat(),
            "news_table": _render_news(state.snapshot.news, max_items=20),
        }


class SentimentAnalyst(Agent):
    """Retail/social sentiment analyst using best-effort chatter inputs."""

    class Payload(AgentPayload):
        retail_mood: Literal["very_negative", "negative", "neutral", "positive", "very_positive"]
        notable_narratives: list[str]
        confidence: int = Field(ge=0, le=100)

    agent_name: ClassVar[str] = "sentiment_analyst"
    tier: ClassVar[Literal["quick"]] = "quick"
    prompt_template: ClassVar[str] = "sentiment_analyst.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[SentimentAnalystReport]] = SentimentAnalystReport

    async def run(self, state: RunState) -> SentimentAnalystReport:
        """Return a neutral no-cost report when no chatter is available."""

        if state.snapshot is not None and state.snapshot.news:
            return await super().run(state)  # type: ignore[return-value]
        model = self._model_for_start()
        await self.bus.publish(AgentStarted(run_id=state.run_id, agent=self.agent_name, model=model))
        report = SentimentAnalystReport(
            run_id=state.run_id,
            agent=self.agent_name,
            model=model,
            created_at=utc_now(),
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            content="No social/news chatter was provided; retail mood defaults to neutral.",
            retail_mood="neutral",
            notable_narratives=[],
            confidence=0,
        )
        await self.bus.publish(AgentCompleted(run_id=state.run_id, report=report))
        return report

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        if state.snapshot is None:
            msg = "SentimentAnalyst requires RunState.snapshot"
            raise ValueError(msg)
        return {
            "run_id": state.run_id,
            "symbol": state.snapshot.symbol,
            "as_of": state.snapshot.as_of.isoformat(),
            "chatter_table": _render_news(state.snapshot.news, max_items=20),
        }


class OptionsAnalyst(Agent):
    """Options-chain analyst using deterministic volatility and liquidity facts."""

    class Payload(AgentPayload):
        iv_regime: Literal["cheap", "fair", "rich"]
        expected_move_pct: float
        skew_note: str
        event_risk: list[str]
        confidence: int = Field(ge=0, le=100)

    agent_name: ClassVar[str] = "options_analyst"
    tier: ClassVar[Literal["quick"]] = "quick"
    prompt_template: ClassVar[str] = "options_analyst.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[OptionsAnalystReport]] = OptionsAnalystReport

    async def run(self, state: RunState) -> OptionsAnalystReport:
        """Return a neutral no-cost report when no option chain is available."""

        if state.option_chain is not None:
            return await super().run(state)  # type: ignore[return-value]
        model = self._model_for_start()
        await self.bus.publish(AgentStarted(run_id=state.run_id, agent=self.agent_name, model=model))
        report = OptionsAnalystReport(
            run_id=state.run_id,
            agent=self.agent_name,
            model=model,
            created_at=utc_now(),
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            content="No option chain was provided; volatility context defaults to neutral.",
            iv_regime="fair",
            expected_move_pct=0.0,
            skew_note="No option chain provided.",
            event_risk=[],
            confidence=0,
        )
        await self.bus.publish(AgentCompleted(run_id=state.run_id, report=report))
        return report

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        if state.option_chain is None:
            msg = "OptionsAnalyst requires RunState.option_chain"
            raise ValueError(msg)
        return {
            "run_id": state.run_id,
            "symbol": state.symbol,
            "as_of": state.as_of.isoformat(),
            "options_facts": _render_options_facts(state),
        }


def _render_frame(path: str, *, max_rows: int) -> str:
    import pandas as pd

    frame = pd.read_parquet(path) if path.lower().endswith(".parquet") else pd.read_csv(path)
    frame = frame.rename(columns={col: str(col) for col in frame.columns})
    frame = _with_date_column(frame)
    if "date" in {str(col).lower() for col in frame.columns}:
        date_col = next(col for col in frame.columns if str(col).lower() == "date")
        frame = frame.sort_values(by=date_col)
    frame = frame.tail(max_rows)
    columns = _ordered_columns([str(col) for col in frame.columns])
    rows = [[_format_cell(row.get(col)) for col in columns] for row in frame.to_dict("records")]
    if not rows:
        return "_No rows provided._"
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def _with_date_column(frame: Any) -> Any:
    import pandas as pd

    columns = {str(col).lower() for col in frame.columns}
    if "date" in columns:
        return frame
    if not isinstance(frame.index, pd.RangeIndex):
        frame = frame.reset_index()
        first_col = str(frame.columns[0])
        return frame.rename(columns={first_col: "date"})
    return frame


def _ordered_columns(columns: list[str]) -> list[str]:
    priority = ["date", "open", "high", "low", "close", "volume"]
    by_lower = {col.lower(): col for col in columns}
    ordered = [by_lower[name] for name in priority if name in by_lower]
    ordered.extend(col for col in columns if col not in ordered)
    return ordered


def _format_cell(value: Any) -> str:
    import pandas as pd

    if value is None or pd.isna(value):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat") and "Timestamp" in type(value).__name__:
        return value.date().isoformat()
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace("|", "\\|")


def _render_fundamentals(snapshot: FundamentalsSnapshot | None) -> str:
    if snapshot is None:
        return "_No fundamentals snapshot provided._"
    rows = [
        ("symbol", snapshot.symbol),
        ("as_of", snapshot.as_of.isoformat()),
        ("market_cap", snapshot.market_cap),
        ("pe_ttm", snapshot.pe_ttm),
        ("forward_pe", snapshot.forward_pe),
        ("eps_ttm", snapshot.eps_ttm),
        ("revenue_growth_yoy", snapshot.revenue_growth_yoy),
        ("profit_margin", snapshot.profit_margin),
        ("debt_to_equity", snapshot.debt_to_equity),
        ("free_cash_flow", snapshot.free_cash_flow),
        (
            "next_earnings_date",
            snapshot.next_earnings_date.isoformat() if snapshot.next_earnings_date else None,
        ),
        ("analyst_target_mean", snapshot.analyst_target_mean),
    ]
    body = ["| field | value |", "| --- | --- |"]
    body.extend(f"| {field} | {_format_cell(value)} |" for field, value in rows)
    return "\n".join(body)


def _render_news(items: list[NewsItem], *, max_items: int) -> str:
    selected = sorted(items, key=lambda item: item.published, reverse=True)[:max_items]
    if not selected:
        return "_No news or chatter items provided._"
    rows = []
    for item in selected:
        rows.append(
            [
                item.published.isoformat(),
                item.source,
                item.title,
                item.summary,
            ]
        )
    header = "| published | source | title | summary |"
    separator = "| --- | --- | --- | --- |"
    body = ["| " + " | ".join(_format_cell(value) for value in row) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def _render_options_facts(state: RunState) -> str:
    chain = state.option_chain
    if chain is None:
        return "_No option chain provided._"
    nearest_expiry = min(chain.expiries, key=lambda expiry: abs((expiry - chain.as_of.date()).days), default=None)
    dte = max((nearest_expiry - chain.as_of.date()).days, 0) if nearest_expiry else 0
    atm_iv = chain.atm_iv
    expected_move = float(chain.spot) * atm_iv * math.sqrt(dte / 365.0) if atm_iv is not None else None
    expected_move_pct = (expected_move / float(chain.spot) * 100.0) if expected_move is not None and chain.spot else None
    iv_by_expiry = _average_iv_by_expiry(state)
    term_slope = None
    if len(iv_by_expiry) >= 2:
        ordered = sorted(iv_by_expiry.items(), key=lambda item: item[0])
        term_slope = ordered[-1][1] - ordered[0][1]
    put_oi = sum(q.open_interest for q in chain.quotes if q.contract.kind == "put")
    call_oi = sum(q.open_interest for q in chain.quotes if q.contract.kind == "call")
    oi_skew = (put_oi / call_oi) if call_oi else None
    earnings_date = state.snapshot.fundamentals.next_earnings_date if state.snapshot and state.snapshot.fundamentals else None
    days_to_earnings = (earnings_date - chain.as_of.date()).days if earnings_date else None
    rows = [
        ("underlying", chain.underlying),
        ("spot", chain.spot),
        ("nearest_expiry", nearest_expiry.isoformat() if nearest_expiry else None),
        ("nearest_dte", dte),
        ("atm_iv", atm_iv),
        ("iv_rank", chain.iv_rank),
        ("iv_percentile", chain.iv_percentile),
        ("rv_yang_zhang", chain.rv_yang_zhang),
        ("iv_minus_rv_yang_zhang", (atm_iv - chain.rv_yang_zhang) if atm_iv is not None and chain.rv_yang_zhang is not None else None),
        ("term_structure_slope_long_minus_short_iv", term_slope),
        ("put_open_interest", put_oi),
        ("call_open_interest", call_oi),
        ("put_call_oi_skew", oi_skew),
        ("expected_move_usd", expected_move),
        ("expected_move_pct", expected_move_pct),
        ("next_earnings_date", earnings_date.isoformat() if earnings_date else None),
        ("days_to_next_earnings", days_to_earnings),
        ("pricing_source", chain.pricing_source),
    ]
    body = ["| fact | value |", "| --- | --- |"]
    body.extend(f"| {name} | {_format_cell(value)} |" for name, value in rows)
    return "\n".join(body)


def _average_iv_by_expiry(state: RunState) -> dict[date, float]:
    chain = state.option_chain
    if chain is None:
        return {}
    result: dict[date, float] = {}
    for expiry in chain.expiries:
        values = [quote.implied_vol for quote in chain.quotes if quote.contract.expiry == expiry and quote.implied_vol is not None]
        if values:
            result[expiry] = sum(values) / len(values)
    return result

"""Analyst agents implemented in Phase 2."""

from __future__ import annotations

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
            "ohlcv_table": _render_frame(state.snapshot.ohlcv_path, max_rows=90),
            "indicator_table": _render_frame(state.snapshot.indicators_path, max_rows=90),
        }


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

"""Shared pydantic models that cross Sentinel package boundaries."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class SentinelModel(BaseModel):
    """Base model for persisted, cross-package contracts."""

    model_config = ConfigDict(validate_assignment=True, arbitrary_types_allowed=False)


class Quote(SentinelModel):
    """Latest price snapshot for one symbol."""

    symbol: str
    price: Decimal
    ts: datetime
    source: str


class NewsItem(SentinelModel):
    """Normalized news item used by news and sentiment agents."""

    title: str
    summary: str
    source: str
    url: str
    published: datetime
    symbols: list[str]


class FundamentalsSnapshot(SentinelModel):
    """Point-in-time fundamentals for one symbol."""

    symbol: str
    as_of: date
    market_cap: float | None
    pe_ttm: float | None
    forward_pe: float | None
    eps_ttm: float | None
    revenue_growth_yoy: float | None
    profit_margin: float | None
    debt_to_equity: float | None
    free_cash_flow: float | None
    next_earnings_date: date | None
    analyst_target_mean: float | None


class DataSnapshot(SentinelModel):
    """Pinned data inputs for a decision run."""

    run_id: str
    symbol: str
    as_of: datetime
    ohlcv_path: str
    indicators_path: str
    news: list[NewsItem]
    fundamentals: FundamentalsSnapshot | None
    providers_used: dict[str, str]


class AgentReport(SentinelModel):
    """Common report envelope emitted by every agent."""

    run_id: str
    agent: str
    model: str
    created_at: datetime
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    content: str


class MarketAnalystReport(AgentReport):
    """Technical-analysis report."""

    trend: Literal["strong_up", "up", "sideways", "down", "strong_down"]
    support: float | None
    resistance: float | None
    signals: list[str]
    confidence: int = Field(ge=0, le=100)


class FundamentalsAnalystReport(AgentReport):
    """Fundamental-analysis report."""

    valuation: Literal["cheap", "fair", "expensive", "unknown"]
    quality_flags: list[str]
    red_flags: list[str]
    upcoming_catalysts: list[str]
    confidence: int = Field(ge=0, le=100)


class NewsAnalystReport(AgentReport):
    """News-analysis report."""

    sentiment: Literal["very_negative", "negative", "neutral", "positive", "very_positive"]
    key_events: list[str]
    macro_context: str
    confidence: int = Field(ge=0, le=100)


class SentimentAnalystReport(AgentReport):
    """Retail/social sentiment report."""

    retail_mood: Literal["very_negative", "negative", "neutral", "positive", "very_positive"]
    notable_narratives: list[str]
    confidence: int = Field(ge=0, le=100)


class DebateTurn(SentinelModel):
    """One evidence-based turn in a bull/bear or risk debate."""

    speaker: Literal["bull", "bear"]
    round: int
    argument: str


class InvestmentPlan(AgentReport):
    """Research Manager's judged investment plan."""

    stance: Literal["bullish", "bearish", "neutral"]
    conviction: int = Field(ge=0, le=100)
    thesis: str
    key_risks: list[str]
    invalidation: str
    debate_scorecard: str


class TradeProposal(AgentReport):
    """Trader's proposed action and sizing."""

    action: Literal["BUY", "SELL", "HOLD"]
    quantity_pct: float = Field(ge=0, le=100)
    order_type: Literal["market"]
    time_horizon_days: int
    entry_rationale: str
    exit_plan: str
    stop_loss_pct: float | None
    take_profit_pct: float | None


class PMDecision(AgentReport):
    """Portfolio Manager advisory verdict before deterministic risk gates."""

    verdict: Literal["APPROVE", "REVISE", "REJECT"]
    approved_quantity_pct: float = Field(ge=0, le=100)
    reasoning: str
    lessons_applied: list[str]


class Order(SentinelModel):
    """Order intent submitted to execution after risk gating."""

    order_id: str
    run_id: str | None
    symbol: str
    side: Literal["buy", "sell"]
    qty: Decimal
    type: Literal["market"]
    reason: Literal["agent_decision", "stop_loss", "take_profit", "time_exit", "manual"]
    created_at: datetime


class Fill(SentinelModel):
    """Execution fill for an order."""

    order_id: str
    price: Decimal
    qty: Decimal
    ts: datetime
    slippage_usd: Decimal
    commission_usd: Decimal


class Position(SentinelModel):
    """Open portfolio position."""

    symbol: str
    qty: Decimal
    avg_cost: Decimal
    stop_loss_pct: float | None
    take_profit_pct: float | None
    opened_at: datetime
    horizon_days: int | None
    source_run_id: str | None

    @computed_field
    @property
    def cost_basis(self) -> Decimal:
        """Position cost basis at average cost."""

        return self.qty * self.avg_cost


class Portfolio(SentinelModel):
    """Paper portfolio state with helper valuation methods."""

    cash: Decimal
    positions: list[Position] = Field(default_factory=list)
    day_pnl: Decimal = Decimal("0")

    @computed_field
    @property
    def equity(self) -> Decimal:
        """Conservative equity using average cost when no live marks are supplied."""

        return self.cash + sum((p.cost_basis for p in self.positions), Decimal("0"))

    def marked_equity(self, marks: dict[str, Decimal]) -> Decimal:
        """Compute equity using caller-provided last prices keyed by symbol."""

        return self.cash + sum(
            (p.qty * marks.get(p.symbol, p.avg_cost) for p in self.positions), Decimal("0")
        )


class Mandate(SentinelModel):
    """Deterministic trading mandate mirrored from mandate.toml."""

    symbol_universe: list[str]
    max_position_pct_equity: float
    max_order_notional_usd: float
    max_gross_exposure_pct: float
    max_daily_loss_pct: float
    max_orders_per_day: int
    allow_short: bool
    cooldown_minutes_per_symbol: int


ViolationCode = Literal[
    "SYMBOL_NOT_IN_UNIVERSE",
    "ORDER_TOO_LARGE",
    "EXPOSURE_CAP",
    "DAILY_LOSS_HALT",
    "COOLDOWN",
    "SHORT_NOT_ALLOWED",
    "KILL_SWITCH",
    "MAX_ORDERS_PER_DAY",
]


class Violation(SentinelModel):
    """Single mandate gate violation."""

    code: ViolationCode
    message: str


class GateResult(SentinelModel):
    """Deterministic mandate gate result."""

    passed: bool
    violations: list[Violation] = Field(default_factory=list)


RunStatus = Literal[
    "pending",
    "fetching_data",
    "analyzing",
    "debating",
    "trading",
    "risk_review",
    "gating",
    "executing",
    "completed",
    "failed",
    "cancelled",
    "halted",
]


class RunState(SentinelModel):
    """Full orchestration state for a decision or backtest step."""

    run_id: str
    symbol: str
    as_of: datetime
    mode: Literal["decision", "backtest_step"]
    status: RunStatus
    snapshot: DataSnapshot | None
    analyst_reports: dict[str, AgentReport] = Field(default_factory=dict)
    debate_transcript: list[DebateTurn] = Field(default_factory=list)
    investment_plan: InvestmentPlan | None
    trade_proposal: TradeProposal | None
    risk_transcript: list[DebateTurn] = Field(default_factory=list)
    pm_decision: PMDecision | None
    final_order: Order | None
    error: str | None
    total_cost_usd: Decimal = Decimal("0")
    total_tokens: int = 0


class Lesson(SentinelModel):
    """Reflection lesson recalled into future prompts."""

    lesson_id: str
    created_at: datetime
    symbol: str
    setup_tags: list[str]
    what_happened: str
    lesson: str
    grade: Literal["good_call", "bad_call", "lucky", "unlucky"]


class JournalEntry(SentinelModel):
    """Decision journal row with realized outcome fields filled later."""

    run_id: str
    symbol: str
    date: date
    stance: Literal["bullish", "bearish", "neutral"]
    action: Literal["BUY", "SELL", "HOLD"]
    conviction: int = Field(ge=0, le=100)
    size: float
    thesis_summary: str
    invalidation: str
    horizon_end: date | None = None
    realized_ret: float | None = None
    bench_ret: float | None = None
    graded: Literal["good_call", "bad_call", "lucky", "unlucky"] | None = None
    reflected_at: datetime | None = None


class BacktestResult(SentinelModel):
    """Backtest metrics and time series output."""

    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_drawdown_start: date | None
    max_drawdown_end: date | None
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    exposure_pct: float
    turnover: float
    trades: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    benchmark_symbol: str
    benchmark_return: float
    alpha: float
    bootstrap_sharpe_p05: float | None = None
    bootstrap_sharpe_p95: float | None = None
    bootstrap_max_drawdown_p05: float | None = None
    bootstrap_max_drawdown_p95: float | None = None


class Signal(SentinelModel):
    """Rule-strategy signal for the backtest engine."""

    action: Literal["BUY", "SELL", "HOLD"]
    size: float | None = None

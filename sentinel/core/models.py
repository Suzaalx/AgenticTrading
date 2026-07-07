"""Shared pydantic models that cross Sentinel package boundaries."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from sentinel.core.ids import new_id


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


OptionKind = Literal["call", "put"]
OptionStrategyId = Literal[
    "long_call",
    "long_put",
    "covered_call",
    "cash_secured_put",
    "bull_call_spread",
    "bear_put_spread",
    "bull_put_spread",
    "bear_call_spread",
    "long_straddle",
    "long_strangle",
    "iron_condor",
    "calendar_spread",
]


class OptionContract(SentinelModel):
    """OCC option contract identity."""

    contract_symbol: str
    underlying: str
    kind: OptionKind
    strike: Decimal
    expiry: date
    multiplier: int = 100


class OptionQuote(SentinelModel):
    """Point-in-time option quote enriched with local analytics."""

    contract: OptionContract
    bid: Decimal
    ask: Decimal
    last: Decimal | None
    volume: int
    open_interest: int
    implied_vol: float | None
    ts: datetime
    source: str
    model_iv: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None


class OptionChainSnapshot(SentinelModel):
    """Pinned option-chain artifact for a decision run."""

    run_id: str
    underlying: str
    as_of: datetime
    spot: Decimal
    risk_free_rate: float
    dividend_yield: float
    expiries: list[date]
    quotes: list[OptionQuote]
    atm_iv: float | None
    iv_rank: float | None
    iv_percentile: float | None
    rv_yang_zhang: float | None
    pricing_source: Literal["live_chain", "synthetic_bsm"]
    providers_used: dict[str, str]


class OptionLeg(SentinelModel):
    """One leg of an option strategy."""

    contract: OptionContract
    side: Literal["buy", "sell"]
    contracts: int
    limit_price: Decimal | None


class OptionStrategyCandidate(SentinelModel):
    """Deterministic option structure offered to agents."""

    strategy: OptionStrategyId
    legs: list[OptionLeg]
    net_premium: Decimal
    max_loss: Decimal
    max_gain: Decimal | None
    breakevens: list[Decimal]
    est_pop: float | None
    net_delta: float
    net_vega: float
    net_theta: float
    liquidity_score: float
    rationale_facts: str


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
    data_quality: Literal["live", "delayed", "synthetic"] = "live"


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


class OptionsAnalystReport(AgentReport):
    """Options-chain context report."""

    iv_regime: Literal["cheap", "fair", "rich"]
    expected_move_pct: float
    skew_note: str
    event_risk: list[str]
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
    debate_won_by: Literal["bull", "bear", "split"] = "split"


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

    @field_validator("stop_loss_pct", "take_profit_pct", mode="before")
    @classmethod
    def _normalize_nullish_float(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in {"", "n/a", "null", "none"}:
            return None
        return value


class OptionStrategyProposal(AgentReport):
    """Trader's proposed option action, parallel to TradeProposal."""

    action: Literal["OPEN", "CLOSE", "HOLD"]
    strategy: OptionStrategyId
    legs: list[OptionLeg]
    candidate_id: str
    max_loss_usd: Decimal
    time_horizon_days: int
    entry_rationale: str
    exit_plan: str
    stop_loss_pct_premium: float | None
    take_profit_pct_premium: float | None


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
    type: Literal["market", "limit", "net_debit", "net_credit"] = "market"
    reason: Literal[
        "agent_decision",
        "stop_loss",
        "take_profit",
        "time_exit",
        "manual",
        "expiry_settlement",
    ]
    created_at: datetime
    asset_type: Literal["equity", "option", "crypto"] = "equity"
    legs: list[OptionLeg] | None = None
    strategy: OptionStrategyId | None = None
    client_order_id: str = Field(default_factory=new_id)
    venue: Literal["paper", "robinhood_crypto", "robinhood_agentic"] = "paper"

    def __eq__(self, other: object) -> bool:
        """Compare persisted order fields while legacy repos omit idempotency keys."""

        if not isinstance(other, Order):
            return NotImplemented
        return self.model_dump(exclude={"client_order_id"}) == other.model_dump(
            exclude={"client_order_id"}
        )


class Fill(SentinelModel):
    """Execution fill for an order."""

    order_id: str
    price: Decimal
    qty: Decimal
    ts: datetime
    slippage_usd: Decimal
    commission_usd: Decimal
    venue: Literal["paper", "robinhood_crypto", "robinhood_agentic"] = "paper"
    broker_order_id: str | None = None


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


class OptionPosition(SentinelModel):
    """Open option strategy position."""

    position_id: str
    underlying: str
    strategy: OptionStrategyId
    legs: list[OptionLeg]
    open_premium: Decimal
    max_loss: Decimal
    collateral: Decimal
    opened_at: datetime
    expiry: date
    horizon_days: int | None
    stop_loss_pct_premium: float | None
    take_profit_pct_premium: float | None
    source_run_id: str | None
    venue: Literal["paper", "robinhood_agentic"] = "paper"


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


class MandateOptions(SentinelModel):
    """Options mandate constraints."""

    enabled: bool = False
    underlying_universe: list[str] = Field(
        default_factory=lambda: ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "SPY", "QQQ"]
    )
    defined_risk_only: bool = True
    max_loss_per_position_usd: float = 500.0
    max_total_options_max_loss_pct_equity: float = 15.0
    max_contracts_per_order: int = 10
    min_open_interest: int = 100
    max_rel_spread_pct: float = 10.0
    min_dte: int = 21
    max_dte: int = 60
    max_net_portfolio_delta_abs: float = 200.0
    max_net_portfolio_vega_abs: float = 500.0
    max_option_orders_per_day: int = 4

    @field_validator("defined_risk_only")
    @classmethod
    def _must_be_defined_risk(cls, value: bool) -> bool:
        if not value:
            msg = "options.defined_risk_only cannot be disabled"
            raise ValueError(msg)
        return value


class MandateLive(SentinelModel):
    """Live-trading mandate overlay."""

    crypto_stage_enabled: bool = False
    equity_stage_enabled: bool = False
    options_stage_enabled: bool = False
    max_live_order_notional_usd: float = 200.0
    max_live_daily_loss_usd: float = 100.0
    max_live_orders_per_day: int = 3
    max_account_allocation_usd: float = 2000.0
    require_limit_orders: bool = True
    quote_max_age_seconds: int = 60


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
    options: MandateOptions = Field(default_factory=MandateOptions)
    live: MandateLive = Field(default_factory=MandateLive)


ViolationCode = Literal[
    "SYMBOL_NOT_IN_UNIVERSE",
    "ORDER_TOO_LARGE",
    "EXPOSURE_CAP",
    "DAILY_LOSS_HALT",
    "COOLDOWN",
    "SHORT_NOT_ALLOWED",
    "KILL_SWITCH",
    "MAX_ORDERS_PER_DAY",
    "OPTIONS_DISABLED",
    "NAKED_SHORT_OPTION",
    "MAX_LOSS_EXCEEDED",
    "OPTIONS_BUDGET_EXCEEDED",
    "ILLIQUID_CONTRACT",
    "DTE_OUT_OF_RANGE",
    "GREEKS_CAP_EXCEEDED",
    "UNDERLYING_NOT_ALLOWED",
    "INSUFFICIENT_COLLATERAL",
    "EXPIRY_TOO_CLOSE",
    "LIVE_STAGE_NOT_ENABLED",
    "LIVE_NOTIONAL_EXCEEDED",
    "LIVE_DAILY_LOSS_HALT",
    "LIVE_ORDER_LIMIT",
    "QUOTE_TOO_OLD",
    "RECONCILIATION_MISMATCH",
    "VENUE_CAPABILITY_MISSING",
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
    option_chain: OptionChainSnapshot | None = None
    option_candidates: list[OptionStrategyCandidate] = Field(default_factory=list)
    option_proposal: OptionStrategyProposal | None = None
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
    action: Literal["BUY", "SELL", "HOLD", "OPEN_OPTION", "CLOSE_OPTION"]
    conviction: int = Field(ge=0, le=100)
    size: float
    thesis_summary: str
    invalidation: str
    horizon_end: date | None = None
    realized_ret: float | None = None
    bench_ret: float | None = None
    graded: Literal["good_call", "bad_call", "lucky", "unlucky"] | None = None
    reflected_at: datetime | None = None
    strategy: OptionStrategyId | None = None
    venue: str = "paper"


class BacktestWalkForwardWindow(SentinelModel):
    """One contiguous return window used for backtest robustness checks."""

    window: int
    start: date | None
    end: date | None
    periods: int
    total_return: float
    sharpe: float
    max_drawdown: float
    consistent: bool


class BacktestResult(SentinelModel):
    """Backtest metrics and time series output."""

    total_return: float
    annualized_return: float
    calmar: float = 0.0
    sharpe: float
    sortino: float
    information_ratio: float = 0.0
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
    walk_forward: list[BacktestWalkForwardWindow] | None = None
    walk_forward_consistency: float | None = None


class Signal(SentinelModel):
    """Rule-strategy signal for the backtest engine."""

    action: Literal["BUY", "SELL", "HOLD"]
    size: float | None = None

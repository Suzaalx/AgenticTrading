from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sentinel.core.models import (
    AgentReport,
    BacktestResult,
    DataSnapshot,
    DebateTurn,
    Fill,
    FundamentalsAnalystReport,
    FundamentalsSnapshot,
    GateResult,
    InvestmentPlan,
    JournalEntry,
    Lesson,
    Mandate,
    MarketAnalystReport,
    NewsAnalystReport,
    NewsItem,
    Order,
    PMDecision,
    Portfolio,
    Position,
    Quote,
    RunState,
    SentimentAnalystReport,
    Signal,
    TradeProposal,
    Violation,
)


def envelope() -> dict[str, Any]:
    return {
        "run_id": "01JTEST",
        "agent": "agent",
        "model": "fake",
        "created_at": datetime.now(UTC),
        "latency_ms": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": Decimal("0.01"),
        "content": "ok",
    }


def test_trade_proposal_nullish_stop_values_become_none() -> None:
    payload: dict[str, Any] = {
        **envelope(),
        "action": "HOLD",
        "quantity_pct": 0.0,
        "order_type": "market",
        "time_horizon_days": 1,
        "entry_rationale": "wait",
        "exit_plan": "none",
        "stop_loss_pct": "N/A",
        "take_profit_pct": " null ",
    }
    proposal = TradeProposal.model_validate(payload)

    assert proposal.stop_loss_pct is None
    assert proposal.take_profit_pct is None


def test_all_models_instantiate() -> None:
    now = datetime.now(UTC)
    quote = Quote(symbol="NVDA", price=Decimal("100"), ts=now, source="fixture")
    news = NewsItem(
        title="t",
        summary="s",
        source="src",
        url="https://example.invalid",
        published=now,
        symbols=["NVDA"],
    )
    fundamentals = FundamentalsSnapshot(
        symbol="NVDA",
        as_of=date.today(),
        market_cap=1.0,
        pe_ttm=None,
        forward_pe=None,
        eps_ttm=None,
        revenue_growth_yoy=None,
        profit_margin=None,
        debt_to_equity=None,
        free_cash_flow=None,
        next_earnings_date=None,
        analyst_target_mean=None,
    )
    snapshot = DataSnapshot(
        run_id="01JTEST",
        symbol="NVDA",
        as_of=now,
        ohlcv_path="ohlcv.parquet",
        indicators_path="ind.parquet",
        news=[news],
        fundamentals=fundamentals,
        providers_used={"ohlcv": "fixture"},
    )
    agent = AgentReport(**envelope())
    market = MarketAnalystReport(
        **envelope(),
        trend="up",
        support=90.0,
        resistance=110.0,
        signals=["s"],
        confidence=80,
    )
    fund = FundamentalsAnalystReport(
        **envelope(),
        valuation="fair",
        quality_flags=[],
        red_flags=[],
        upcoming_catalysts=[],
        confidence=70,
    )
    news_report = NewsAnalystReport(
        **envelope(), sentiment="neutral", key_events=[], macro_context="none", confidence=50
    )
    sentiment = SentimentAnalystReport(
        **envelope(), retail_mood="neutral", notable_narratives=[], confidence=0
    )
    turn = DebateTurn(speaker="bull", round=1, argument="arg")
    plan = InvestmentPlan(
        **envelope(),
        stance="bullish",
        conviction=75,
        thesis="thesis",
        key_risks=[],
        invalidation="break",
        debate_scorecard="bull",
    )
    assert plan.debate_won_by == "split"
    proposal = TradeProposal(
        **envelope(),
        action="BUY",
        quantity_pct=50.0,
        order_type="market",
        time_horizon_days=10,
        entry_rationale="r",
        exit_plan="x",
        stop_loss_pct=8.0,
        take_profit_pct=12.0,
    )
    pm = PMDecision(
        **envelope(),
        verdict="APPROVE",
        approved_quantity_pct=40.0,
        reasoning="ok",
        lessons_applied=[],
    )
    violation = Violation(code="ORDER_TOO_LARGE", message="too large")
    gate = GateResult(passed=False, violations=[violation])
    order = Order(
        order_id="01O",
        run_id="01JTEST",
        symbol="NVDA",
        side="buy",
        qty=Decimal("1"),
        type="market",
        reason="agent_decision",
        created_at=now,
    )
    fill = Fill(
        order_id="01O",
        price=Decimal("100"),
        qty=Decimal("1"),
        ts=now,
        slippage_usd=Decimal("0"),
        commission_usd=Decimal("0"),
    )
    position = Position(
        symbol="NVDA",
        qty=Decimal("1"),
        avg_cost=Decimal("100"),
        stop_loss_pct=8.0,
        take_profit_pct=12.0,
        opened_at=now,
        horizon_days=10,
        source_run_id="01JTEST",
    )
    portfolio = Portfolio(cash=Decimal("900"), positions=[position])
    mandate = Mandate(
        symbol_universe=["NVDA"],
        max_position_pct_equity=10.0,
        max_order_notional_usd=2000.0,
        max_gross_exposure_pct=80.0,
        max_daily_loss_pct=3.0,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
    )
    state = RunState(
        run_id="01JTEST",
        symbol="NVDA",
        as_of=now,
        mode="decision",
        status="pending",
        snapshot=snapshot,
        analyst_reports={"market": market},
        debate_transcript=[turn],
        investment_plan=plan,
        trade_proposal=proposal,
        risk_transcript=[turn],
        pm_decision=pm,
        final_order=order,
        error=None,
        total_cost_usd=Decimal("0.02"),
        total_tokens=2,
    )
    lesson = Lesson(
        lesson_id="01L",
        created_at=now,
        symbol="NVDA",
        setup_tags=["tag"],
        what_happened="up",
        lesson="Respect risk.",
        grade="good_call",
    )
    journal = JournalEntry(
        run_id="01JTEST",
        symbol="NVDA",
        date=date.today(),
        stance="bullish",
        action="BUY",
        conviction=75,
        size=40.0,
        thesis_summary="t",
        invalidation="i",
    )
    backtest = BacktestResult(
        total_return=1.0,
        annualized_return=1.0,
        sharpe=1.0,
        sortino=1.0,
        max_drawdown=0.1,
        max_drawdown_start=None,
        max_drawdown_end=None,
        win_rate=0.5,
        profit_factor=1.2,
        avg_win=0.1,
        avg_loss=-0.1,
        exposure_pct=50.0,
        turnover=1.0,
        trades=[],
        equity_curve=[],
        benchmark_symbol="SPY",
        benchmark_return=0.5,
        alpha=0.5,
    )
    signal = Signal(action="HOLD", size=None)
    assert quote.price == Decimal("100")
    assert agent.content == "ok"
    assert fund.valuation == "fair"
    assert news_report.sentiment == "neutral"
    assert sentiment.retail_mood == "neutral"
    assert gate.violations[0] == violation
    assert fill.order_id == order.order_id
    assert portfolio.equity == Decimal("1000")
    assert mandate.symbol_universe == ["NVDA"]
    assert state.status == "pending"
    assert lesson.grade == "good_call"
    assert journal.action == "BUY"
    assert backtest.alpha == 0.5
    assert signal.action == "HOLD"

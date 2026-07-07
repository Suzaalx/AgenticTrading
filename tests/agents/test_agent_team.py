from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.agents.analysts import FundamentalsAnalyst, NewsAnalyst, SentimentAnalyst
from sentinel.agents.portfolio_manager import PortfolioManager
from sentinel.agents.researchers import (
    BearResearcher,
    BullResearcher,
    ResearchManager,
    run_research_debate,
)
from sentinel.agents.risk_debaters import (
    AggressiveRisk,
    ConservativeRisk,
    NeutralRisk,
    run_risk_debate,
)
from sentinel.agents.trader import Trader
from sentinel.config.settings import LLMSettings, Settings
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentCompleted, AgentStarted, DebateTurnAdded
from sentinel.core.models import (
    DataSnapshot,
    FundamentalsAnalystReport,
    FundamentalsSnapshot,
    InvestmentPlan,
    Lesson,
    MarketAnalystReport,
    NewsAnalystReport,
    NewsItem,
    PMDecision,
    Portfolio,
    Position,
    RunState,
    SentimentAnalystReport,
    TradeProposal,
)
from sentinel.llm.budget import BudgetExceeded
from sentinel.llm.contracts import LLMResult
from sentinel.store.db import connect, run_migrations
from sentinel.testing.fakes import FakeLLM


def _envelope(agent: str = "agent") -> dict[str, Any]:
    return {
        "run_id": "run_agents",
        "agent": agent,
        "model": "fake-model",
        "created_at": datetime.now(UTC),
        "latency_ms": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": Decimal("0"),
        "content": "ok",
    }


def _snapshot(*, news: list[NewsItem] | None = None) -> DataSnapshot:
    now = datetime(2026, 7, 6, tzinfo=UTC)
    item = NewsItem(
        title="NVDA announces demand update",
        summary="Management cited strong data-center demand.",
        source="fixture",
        url="https://example.invalid/news",
        published=now - timedelta(hours=2),
        symbols=["NVDA"],
    )
    fundamentals = FundamentalsSnapshot(
        symbol="NVDA",
        as_of=date(2026, 7, 6),
        market_cap=3_000_000_000_000.0,
        pe_ttm=45.0,
        forward_pe=32.0,
        eps_ttm=4.25,
        revenue_growth_yoy=0.42,
        profit_margin=0.55,
        debt_to_equity=0.2,
        free_cash_flow=40_000_000_000.0,
        next_earnings_date=date(2026, 8, 20),
        analyst_target_mean=160.0,
    )
    return DataSnapshot(
        run_id="run_agents",
        symbol="NVDA",
        as_of=now,
        ohlcv_path="unused.parquet",
        indicators_path="unused.parquet",
        news=[item] if news is None else news,
        fundamentals=fundamentals,
        providers_used={"news": "fixture", "fundamentals": "fixture"},
    )


def _market_report() -> MarketAnalystReport:
    return MarketAnalystReport(
        **_envelope("market_analyst"),
        trend="up",
        support=140.0,
        resistance=155.0,
        signals=["close 150.0 above SMA 20 value 145.0"],
        confidence=70,
    )


def _investment_plan() -> InvestmentPlan:
    return InvestmentPlan(
        **_envelope("research_manager"),
        stance="bullish",
        conviction=78,
        thesis="Demand and trend support a long. Valuation is the key constraint.",
        key_risks=["valuation"],
        invalidation="Revenue growth falls below provided 0.42 value.",
        debate_scorecard="Bull won on demand; bear won on valuation risk.",
        debate_won_by="bull",
    )


def _trade_proposal(action: str = "BUY", quantity_pct: float = 50.0) -> TradeProposal:
    return TradeProposal(
        **_envelope("trader"),
        action=action,  # type: ignore[arg-type]
        quantity_pct=quantity_pct,
        order_type="market",
        time_horizon_days=20,
        entry_rationale="Enter after plan conviction 78.",
        exit_plan="Exit on invalidation or targets.",
        stop_loss_pct=8.0 if action != "HOLD" else None,
        take_profit_pct=15.0 if action != "HOLD" else None,
    )


def _portfolio() -> Portfolio:
    return Portfolio(
        cash=Decimal("10000"),
        positions=[
            Position(
                symbol="NVDA",
                qty=Decimal("5"),
                avg_cost=Decimal("120"),
                stop_loss_pct=8.0,
                take_profit_pct=15.0,
                opened_at=datetime(2026, 6, 1, tzinfo=UTC),
                horizon_days=20,
                source_run_id="old_run",
            )
        ],
    )


def _lesson() -> Lesson:
    return Lesson(
        lesson_id="lesson-1",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        symbol="NVDA",
        setup_tags=["earnings", "momentum"],
        what_happened="Gap reversed after crowded optimism.",
        lesson="Trim size after earnings gaps.",
        grade="bad_call",
    )


def _state(*, news: list[NewsItem] | None = None) -> RunState:
    now = datetime(2026, 7, 6, tzinfo=UTC)
    return RunState(
        run_id="run_agents",
        symbol="NVDA",
        as_of=now,
        mode="decision",
        status="analyzing",
        snapshot=_snapshot(news=news),
        analyst_reports={"market": _market_report()},
        investment_plan=None,
        trade_proposal=None,
        pm_decision=None,
        final_order=None,
        error=None,
    )


def _conn(tmp_path):
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    return conn


@pytest.mark.asyncio
async def test_remaining_analysts_return_typed_reports_and_emit_events(tmp_path) -> None:
    llm = FakeLLM(
        {
            "fundamentals_analyst": {
                "content": "Forward PE 32.0 is below trailing PE 45.0.",
                "valuation": "fair",
                "quality_flags": ["profit_margin 0.55"],
                "red_flags": ["pe_ttm 45.0"],
                "upcoming_catalysts": ["next_earnings_date 2026-08-20"],
                "confidence": 76,
            },
            "news_analyst": {
                "content": "Coverage is positive but thin.",
                "sentiment": "positive",
                "key_events": ["Demand update"],
                "macro_context": "No macro data provided.",
                "confidence": 60,
            },
            "sentiment_analyst": {
                "content": "Chatter leans positive.",
                "retail_mood": "positive",
                "notable_narratives": ["AI demand"],
                "confidence": 50,
            },
        }
    )
    bus = EventBus()
    queue = await bus.subscribe_queue()
    conn = _conn(tmp_path)
    state = _state()

    fundamentals = await FundamentalsAnalyst(llm=llm, bus=bus, conn=conn).run(state)
    news = await NewsAnalyst(llm=llm, bus=bus, conn=conn).run(state)
    sentiment = await SentimentAnalyst(llm=llm, bus=bus, conn=conn).run(state)

    assert isinstance(fundamentals, FundamentalsAnalystReport)
    assert isinstance(news, NewsAnalystReport)
    assert isinstance(sentiment, SentimentAnalystReport)
    assert fundamentals.valuation == "fair"
    assert news.sentiment == "positive"
    assert sentiment.retail_mood == "positive"
    assert "pe_ttm" in llm.calls[0].prompt
    events = [await queue.get() for _ in range(9)]
    assert sum(isinstance(event, AgentStarted) for event in events) == 3
    assert sum(isinstance(event, AgentCompleted) for event in events) == 3


@pytest.mark.asyncio
async def test_sentiment_empty_input_short_circuits_without_llm_call(tmp_path) -> None:
    llm = FakeLLM({})
    bus = EventBus()
    queue = await bus.subscribe_queue()
    state = _state(news=[])

    report = await SentimentAnalyst(llm=llm, bus=bus, conn=_conn(tmp_path)).run(state)

    assert isinstance(report, SentimentAnalystReport)
    assert report.retail_mood == "neutral"
    assert report.confidence == 0
    assert llm.calls == []
    assert isinstance(await queue.get(), AgentStarted)
    assert isinstance(await queue.get(), AgentCompleted)


@pytest.mark.asyncio
async def test_research_debate_alternates_bull_first_and_manager_judges(tmp_path) -> None:
    llm = FakeLLM(
        {
            "bull_researcher": {"argument": "Bull cites trend up and support 140.0."},
            "bear_researcher": {"argument": "Bear rebuts with valuation risk pe_ttm 45.0."},
            "research_manager": {
                "content": "Bull evidence slightly outweighs valuation risk.",
                "stance": "bullish",
                "conviction": 72,
                "thesis": "Trend and demand support a long while valuation caps size.",
                "key_risks": ["pe_ttm 45.0"],
                "invalidation": "Break support 140.0.",
                "debate_scorecard": "Bull trend argument decisive; bear valuation reduced conviction.",
                "debate_won_by": "bull",
            },
        }
    )
    bus = EventBus()
    queue = await bus.subscribe_queue()
    conn = _conn(tmp_path)
    state = _state()
    lesson = _lesson()

    transcript = await run_research_debate(
        state,
        BullResearcher(llm=llm, bus=bus, conn=conn),
        BearResearcher(llm=llm, bus=bus, conn=conn),
        max_rounds=2,
        lessons=[lesson],
    )
    manager = await ResearchManager(llm=llm, bus=bus, conn=conn, lessons=[lesson]).run(state)

    assert [turn.speaker for turn in transcript] == ["bull", "bear", "bull", "bear"]
    assert len(state.debate_transcript) == 4
    assert isinstance(manager, InvestmentPlan)
    assert "decisive" in manager.debate_scorecard
    assert manager.debate_won_by == "bull"
    assert "### Lessons from your past trades" in llm.calls[0].prompt
    assert "Trim size after earnings gaps." in llm.calls[0].prompt
    debate_events = []
    while not queue.empty():
        event = await queue.get()
        if isinstance(event, DebateTurnAdded):
            debate_events.append(event)
    assert len(debate_events) == 4
    assert all(event.debate == "research" for event in debate_events)


@pytest.mark.asyncio
async def test_research_debate_convergence_exits_after_first_full_round(tmp_path) -> None:
    llm = FakeLLM(
        {
            "bull_researcher": {"argument": "Repeated bull point."},
            "bear_researcher": {"argument": "Repeated bear point."},
            "debate_convergence_classifier": {"answer": "no"},
        }
    )
    conn = _conn(tmp_path)
    state = _state()

    transcript = await run_research_debate(
        state,
        BullResearcher(llm=llm, bus=EventBus(), conn=conn),
        BearResearcher(llm=llm, bus=EventBus(), conn=conn),
        max_rounds=3,
    )

    assert len(transcript) == 2
    assert [call.agent for call in llm.calls] == [
        "bull_researcher",
        "bear_researcher",
        "debate_convergence_classifier",
    ]
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM costs WHERE agent = 'debate_convergence_classifier'"
    ).fetchone()
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_research_debate_convergence_classifier_honors_budget_stop(tmp_path) -> None:
    def metered_payload(
        _agent: str,
        _prompt: str,
        schema: type[Any] | None,
        _kwargs: dict[str, Any],
    ) -> LLMResult[Any]:
        assert schema is not None
        structured = schema(**{"argument": "Material point."})
        return LLMResult(
            content="Material point.",
            structured=structured,
            input_tokens=1,
            output_tokens=0,
            model="gpt-5.4-mini",
        )

    llm = FakeLLM(
        {
            "bull_researcher": metered_payload,
            "bear_researcher": metered_payload,
            "debate_convergence_classifier": {"answer": "yes"},
        }
    )
    conn = _conn(tmp_path)
    settings = Settings(llm=LLMSettings(monthly_budget_usd=0.00000050))
    state = _state()

    with pytest.raises(BudgetExceeded):
        await run_research_debate(
            state,
            BullResearcher(llm=llm, bus=EventBus(), conn=conn, settings=settings),
            BearResearcher(llm=llm, bus=EventBus(), conn=conn, settings=settings),
            max_rounds=2,
        )

    assert [call.agent for call in llm.calls] == ["bull_researcher", "bear_researcher"]
    row = conn.execute("SELECT COUNT(*) AS count FROM costs").fetchone()
    assert row["count"] == 2


@pytest.mark.asyncio
async def test_trader_produces_hold_signal_for_orchestrator_and_injects_lessons(tmp_path) -> None:
    llm = FakeLLM(
        {
            "trader": {
                "content": "Neutralized risk means hold.",
                "action": "HOLD",
                "quantity_pct": 0.0,
                "order_type": "market",
                "time_horizon_days": 0,
                "entry_rationale": "No entry because evidence is insufficient.",
                "exit_plan": "No position.",
                "stop_loss_pct": None,
                "take_profit_pct": None,
            }
        }
    )
    state = _state()
    state.investment_plan = _investment_plan()

    proposal = await Trader(
        llm=llm,
        bus=EventBus(),
        conn=_conn(tmp_path),
        portfolio=_portfolio(),
        lessons=[_lesson()],
    ).run(state)

    assert isinstance(proposal, TradeProposal)
    assert proposal.action == "HOLD"
    assert proposal.quantity_pct == 0.0
    assert "Trim size after earnings gaps." in llm.calls[0].prompt


@pytest.mark.asyncio
async def test_risk_debate_runs_three_perspectives_in_order_and_pm_revises(tmp_path) -> None:
    llm = FakeLLM(
        {
            "aggressive_risk": {"argument": "Approve because conviction is 78."},
            "conservative_risk": {"argument": "Cut size because stop_loss_pct is 8.0."},
            "neutral_risk": {"argument": "Balanced view supports a smaller trade."},
            "portfolio_manager": {
                "content": "Approve only at a reduced size.",
                "verdict": "REVISE",
                "approved_quantity_pct": 25.0,
                "reasoning": "Cut from 50.0 due to 8.0 stop risk.",
                "lessons_applied": ["lesson-1"],
            },
        }
    )
    bus = EventBus()
    queue = await bus.subscribe_queue()
    conn = _conn(tmp_path)
    state = _state()
    state.investment_plan = _investment_plan()
    state.trade_proposal = _trade_proposal(quantity_pct=50.0)

    transcript = await run_risk_debate(
        state,
        AggressiveRisk(llm=llm, bus=bus, conn=conn, portfolio=_portfolio()),
        ConservativeRisk(llm=llm, bus=bus, conn=conn, portfolio=_portfolio()),
        NeutralRisk(llm=llm, bus=bus, conn=conn, portfolio=_portfolio()),
        max_rounds=1,
    )
    decision = await PortfolioManager(
        llm=llm,
        bus=bus,
        conn=conn,
        portfolio=_portfolio(),
        lessons=[_lesson()],
    ).run(state)

    assert [call.agent for call in llm.calls[:3]] == [
        "aggressive_risk",
        "conservative_risk",
        "neutral_risk",
    ]
    assert [turn.argument.split()[0] for turn in transcript] == [
        "[aggressive]",
        "[conservative]",
        "[neutral]",
    ]
    assert isinstance(decision, PMDecision)
    assert decision.verdict == "REVISE"
    assert decision.approved_quantity_pct == 25.0
    pm_prompt = next(call.prompt for call in llm.calls if call.agent == "portfolio_manager")
    assert "### Lessons from your past trades" in pm_prompt
    assert "Trim size after earnings gaps." in pm_prompt
    debate_events = []
    while not queue.empty():
        event = await queue.get()
        if isinstance(event, DebateTurnAdded):
            debate_events.append(event)
    assert len(debate_events) == 3
    assert all(event.debate == "risk" for event in debate_events)

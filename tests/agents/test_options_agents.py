from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.agents.analysts import OptionsAnalyst
from sentinel.agents.reflection import ReflectionAgent
from sentinel.agents.trader import Trader
from sentinel.core.bus import EventBus
from sentinel.core.models import (
    DataSnapshot,
    FundamentalsSnapshot,
    InvestmentPlan,
    JournalEntry,
    OptionChainSnapshot,
    OptionContract,
    OptionLeg,
    OptionQuote,
    OptionsAnalystReport,
    OptionStrategyCandidate,
    OptionStrategyProposal,
    RunState,
    TradeProposal,
)
from sentinel.llm.gateway import SchemaParseError
from sentinel.store.db import connect, run_migrations
from sentinel.testing.fakes import FakeLLM


def _conn(tmp_path):
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    return conn


def _envelope(agent: str) -> dict[str, Any]:
    return {
        "run_id": "run_options",
        "agent": agent,
        "model": "fake-model",
        "created_at": datetime.now(UTC),
        "latency_ms": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": Decimal("0"),
        "content": "ok",
    }


def _snapshot(now: datetime) -> DataSnapshot:
    return DataSnapshot(
        run_id="run_options",
        symbol="NVDA",
        as_of=now,
        ohlcv_path="unused.parquet",
        indicators_path="unused.parquet",
        news=[],
        fundamentals=FundamentalsSnapshot(
            symbol="NVDA",
            as_of=now.date(),
            market_cap=None,
            pe_ttm=None,
            forward_pe=None,
            eps_ttm=None,
            revenue_growth_yoy=None,
            profit_margin=None,
            debt_to_equity=None,
            free_cash_flow=None,
            next_earnings_date=now.date() + timedelta(days=10),
            analyst_target_mean=None,
        ),
        providers_used={},
    )


def _chain(now: datetime) -> OptionChainSnapshot:
    expiry = now.date() + timedelta(days=30)
    far_expiry = now.date() + timedelta(days=58)
    quotes: list[OptionQuote] = []
    for exp, iv in [(expiry, 0.42), (far_expiry, 0.46)]:
        for kind, delta in [("call", 0.55), ("put", -0.45)]:
            strike = Decimal("150") if kind == "call" else Decimal("145")
            contract = OptionContract(
                contract_symbol=f"NVDA{exp:%y%m%d}{kind[0].upper()}{int(strike):08d}",
                underlying="NVDA",
                kind=kind,  # type: ignore[arg-type]
                strike=strike,
                expiry=exp,
            )
            quotes.append(
                OptionQuote(
                    contract=contract,
                    bid=Decimal("4.90"),
                    ask=Decimal("5.10"),
                    last=Decimal("5.00"),
                    volume=250,
                    open_interest=500 if kind == "put" else 400,
                    implied_vol=iv,
                    ts=now,
                    source="fixture",
                    model_iv=iv,
                    delta=delta,
                    gamma=0.02,
                    vega=0.14,
                    theta=-0.03,
                )
            )
    return OptionChainSnapshot(
        run_id="run_options",
        underlying="NVDA",
        as_of=now,
        spot=Decimal("150"),
        risk_free_rate=0.04,
        dividend_yield=0.0,
        expiries=[expiry, far_expiry],
        quotes=quotes,
        atm_iv=0.42,
        iv_rank=0.72,
        iv_percentile=0.80,
        rv_yang_zhang=0.30,
        pricing_source="live_chain",
        providers_used={"options": "fixture"},
    )


def _candidate(chain: OptionChainSnapshot) -> OptionStrategyCandidate:
    call = next(q for q in chain.quotes if q.contract.kind == "call")
    put = next(q for q in chain.quotes if q.contract.kind == "put")
    legs = [
        OptionLeg(contract=put.contract, side="sell", contracts=1, limit_price=put.bid),
        OptionLeg(contract=call.contract, side="buy", contracts=1, limit_price=call.ask),
    ]
    return OptionStrategyCandidate(
        strategy="bull_put_spread",
        legs=legs,
        net_premium=Decimal("-120"),
        max_loss=Decimal("380"),
        max_gain=Decimal("120"),
        breakevens=[Decimal("143.80")],
        est_pop=0.61,
        net_delta=0.18,
        net_vega=-0.05,
        net_theta=0.03,
        liquidity_score=0.82,
        rationale_facts="bullish bull_put_spread; iv_rank=0.72; all legs pass filters",
    )


def _state(*, with_chain: bool = True, with_candidate: bool = True) -> RunState:
    now = datetime(2026, 7, 6, tzinfo=UTC)
    chain = _chain(now) if with_chain else None
    state = RunState(
        run_id="run_options",
        symbol="NVDA",
        as_of=now,
        mode="decision",
        status="analyzing",
        snapshot=_snapshot(now),
        option_chain=chain,
        investment_plan=InvestmentPlan(
            **_envelope("research_manager"),
            stance="bullish",
            conviction=80,
            thesis="Trend supports defined-risk options.",
            key_risks=["IV rich"],
            invalidation="Breaks support.",
            debate_scorecard="Bull won.",
        ),
        trade_proposal=None,
        pm_decision=None,
        final_order=None,
        error=None,
    )
    if chain is not None and with_candidate:
        state.option_candidates = [_candidate(chain)]
    return state


@pytest.mark.asyncio
async def test_options_analyst_and_trader_option_proposal(tmp_path) -> None:
    llm = FakeLLM(
        {
            "options_analyst": {
                "content": "ATM IV 0.42 and IV rank 0.72 make volatility rich.",
                "iv_regime": "rich",
                "expected_move_pct": 12.04,
                "skew_note": "Put OI exceeds call OI.",
                "event_risk": ["earnings in 10 days"],
                "confidence": 74,
            },
            "trader": {
                "content": "Open offered candidate_1.",
                "action": "OPEN",
                "strategy": "bull_put_spread",
                "legs": [],
                "candidate_id": "candidate_1",
                "max_loss_usd": "380",
                "time_horizon_days": 30,
                "entry_rationale": "Use defined-risk candidate_1.",
                "exit_plan": "Exit by expiry or risk targets.",
                "stop_loss_pct_premium": 50.0,
                "take_profit_pct_premium": 50.0,
            },
        }
    )
    state = _state()

    report = await OptionsAnalyst(llm=llm, bus=EventBus(), conn=_conn(tmp_path)).run(state)
    proposal = await Trader(llm=llm, bus=EventBus(), conn=_conn(tmp_path)).run(state)

    assert isinstance(report, OptionsAnalystReport)
    assert report.iv_regime == "rich"
    assert isinstance(proposal, OptionStrategyProposal)
    assert proposal.candidate_id == "candidate_1"
    assert proposal.legs == state.option_candidates[0].legs
    assert state.option_proposal == proposal
    assert "max_loss" in llm.calls[1].prompt


@pytest.mark.asyncio
async def test_trader_schema_repair_after_malformed_response(tmp_path) -> None:
    responses = [
        {
            "content": "Malformed equity proposal.",
            "action": "BUY",
            "quantity_pct": 10.0,
            "time_horizon_days": 5,
            "entry_rationale": "Missing order_type.",
            "exit_plan": "Exit.",
        },
        {
            "content": "Repaired hold.",
            "action": "HOLD",
            "quantity_pct": 0.0,
            "order_type": "market",
            "time_horizon_days": 0,
            "entry_rationale": "No trade.",
            "exit_plan": "No position.",
            "stop_loss_pct": None,
            "take_profit_pct": None,
        },
    ]

    def response(*_args: Any) -> dict[str, Any]:
        return responses.pop(0)

    llm = FakeLLM({"trader": response})

    proposal = await Trader(llm=llm, bus=EventBus(), conn=_conn(tmp_path)).run(_state(with_candidate=False))

    assert isinstance(proposal, TradeProposal)
    assert proposal.action == "HOLD"
    assert len(llm.calls) == 2
    assert "failed validation" in llm.calls[1].prompt


@pytest.mark.asyncio
async def test_trader_rejects_bad_candidate_id_and_repairs(tmp_path) -> None:
    responses = [
        {
            "content": "Bad id.",
            "action": "OPEN",
            "strategy": "bull_put_spread",
            "legs": [],
            "candidate_id": "invented",
            "max_loss_usd": "380",
            "time_horizon_days": 30,
            "entry_rationale": "Bad.",
            "exit_plan": "Exit.",
            "stop_loss_pct_premium": None,
            "take_profit_pct_premium": None,
        },
        {
            "content": "Corrected id.",
            "action": "OPEN",
            "strategy": "bull_put_spread",
            "legs": [],
            "candidate_id": "candidate_1",
            "max_loss_usd": "380",
            "time_horizon_days": 30,
            "entry_rationale": "Use offered candidate.",
            "exit_plan": "Exit.",
            "stop_loss_pct_premium": None,
            "take_profit_pct_premium": None,
        },
    ]
    llm = FakeLLM({"trader": lambda *_args: responses.pop(0)})
    state = _state()

    proposal = await Trader(llm=llm, bus=EventBus(), conn=_conn(tmp_path)).run(state)

    assert isinstance(proposal, OptionStrategyProposal)
    assert proposal.candidate_id == "candidate_1"
    assert len(llm.calls) == 2
    assert "was not offered" in llm.calls[1].prompt


@pytest.mark.asyncio
async def test_trader_bad_candidate_id_fails_after_retry(tmp_path) -> None:
    llm = FakeLLM(
        {
            "trader": {
                "content": "Bad id.",
                "action": "OPEN",
                "strategy": "bull_put_spread",
                "legs": [],
                "candidate_id": "invented",
                "max_loss_usd": "380",
                "time_horizon_days": 30,
                "entry_rationale": "Bad.",
                "exit_plan": "Exit.",
                "stop_loss_pct_premium": None,
                "take_profit_pct_premium": None,
            }
        }
    )

    with pytest.raises(SchemaParseError):
        await Trader(llm=llm, bus=EventBus(), conn=_conn(tmp_path)).run(_state())


@pytest.mark.asyncio
async def test_options_analyst_empty_chain_skips_llm(tmp_path) -> None:
    llm = FakeLLM({})

    report = await OptionsAnalyst(llm=llm, bus=EventBus(), conn=_conn(tmp_path)).run(_state(with_chain=False))

    assert isinstance(report, OptionsAnalystReport)
    assert report.confidence == 0
    assert report.iv_regime == "fair"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_reflection_options_grades_return_on_risk_and_tags(tmp_path) -> None:
    entry = JournalEntry(
        run_id="run_options",
        symbol="NVDA",
        date=date(2026, 7, 6),
        stance="bullish",
        action="OPEN_OPTION",
        conviction=80,
        size=380.0,
        thesis_summary="Defined-risk options.",
        invalidation="Breaks support.",
        realized_ret=95.0,
        bench_ret=0.05,
        strategy="bull_put_spread",
        venue="paper",
    )
    llm = FakeLLM(
        {
            "reflection": {
                "content": "Option trade worked.",
                "setup_tags": ["earnings_runup"],
                "what_happened": "P/L was positive.",
                "lesson": "Prefer defined-risk structures when IV is high.",
                "grade": "bad_call",
            }
        }
    )
    state = _state()

    report = await ReflectionAgent(
        llm=llm,
        bus=EventBus(),
        conn=_conn(tmp_path),
        journal_entry=entry,
        bench_ret=entry.bench_ret,
    ).run(state)

    assert report.grade == "good_call"
    assert "strategy:bull_put_spread" in report.setup_tags
    assert "iv_rank:high" in report.setup_tags
    assert "venue:paper" in report.setup_tags
    assert "option_return_on_risk" in llm.calls[0].prompt

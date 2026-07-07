from __future__ import annotations

from decimal import Decimal

import pandas as pd

from sentinel.backtest.engine import BacktestConfig, run_backtest_async
from sentinel.core.models import (
    DataSnapshot,
    OptionChainSnapshot,
    OptionLeg,
    OptionQuote,
    OptionStrategyProposal,
    Signal,
)


async def test_agent_pipeline_receives_synthetic_option_chain_when_enabled() -> None:
    seen: list[OptionChainSnapshot | None] = []

    async def fake_pipeline(
        snapshot: DataSnapshot,
        *,
        option_chain: OptionChainSnapshot | None = None,
    ) -> Signal:
        _ = snapshot
        seen.append(option_chain)
        return Signal(action="HOLD", size=0.0)

    await run_backtest_async(_config(), pipeline=fake_pipeline)

    assert seen
    assert all(chain is not None for chain in seen)
    assert {chain.pricing_source for chain in seen if chain is not None} == {"synthetic_bsm"}


async def test_agent_option_proposal_opens_synthetic_option_position() -> None:
    returned_open = False

    async def fake_pipeline(
        snapshot: DataSnapshot,
        *,
        option_chain: OptionChainSnapshot | None = None,
    ) -> OptionStrategyProposal | Signal:
        nonlocal returned_open
        assert option_chain is not None
        if returned_open:
            return Signal(action="HOLD", size=0.0)
        returned_open = True
        quote = _atm_call(option_chain)
        return OptionStrategyProposal(
            run_id=snapshot.run_id,
            agent="trader",
            model="fake",
            created_at=snapshot.as_of,
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            content="Open deterministic long call.",
            action="OPEN",
            strategy="long_call",
            legs=[OptionLeg(contract=quote.contract, side="buy", contracts=1, limit_price=quote.ask)],
            candidate_id="candidate_1",
            max_loss_usd=quote.ask * Decimal("100"),
            time_horizon_days=30,
            entry_rationale="Fixture option replay.",
            exit_plan="Hold for test horizon.",
            stop_loss_pct_premium=None,
            take_profit_pct_premium=None,
        )

    result = await run_backtest_async(_config(starting_cash=10_000.0), pipeline=fake_pipeline)

    assert any(point["option_position_count"] > 0 for point in result.equity_curve)
    assert any(trade.get("pricing_source") == "synthetic_bsm" for trade in result.trades)
    assert min(point["cash"] for point in result.equity_curve) >= 0


async def test_agent_option_backtest_is_deterministic() -> None:
    async def fake_pipeline(
        snapshot: DataSnapshot,
        *,
        option_chain: OptionChainSnapshot | None = None,
    ) -> OptionStrategyProposal:
        assert option_chain is not None
        quote = _atm_call(option_chain)
        return OptionStrategyProposal(
            run_id=snapshot.run_id,
            agent="trader",
            model="fake",
            created_at=snapshot.as_of,
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            content="Open deterministic long call.",
            action="OPEN",
            strategy="long_call",
            legs=[OptionLeg(contract=quote.contract, side="buy", contracts=1, limit_price=quote.ask)],
            candidate_id="candidate_1",
            max_loss_usd=quote.ask * Decimal("100"),
            time_horizon_days=30,
            entry_rationale="Fixture option replay.",
            exit_plan="Hold for test horizon.",
            stop_loss_pct_premium=None,
            take_profit_pct_premium=None,
        )

    first = await run_backtest_async(_config(bt_id="deterministic_a"), pipeline=fake_pipeline)
    second = await run_backtest_async(_config(bt_id="deterministic_b"), pipeline=fake_pipeline)

    assert first.model_dump() == second.model_dump()


async def test_equity_agent_mode_still_accepts_snapshot_only_pipeline() -> None:
    seen: list[pd.Timestamp] = []

    async def fake_pipeline(snapshot: DataSnapshot) -> Signal:
        seen.append(pd.Timestamp(snapshot.as_of))
        return Signal(action="BUY" if len(seen) == 1 else "HOLD", size=1.0)

    result = await run_backtest_async(
        BacktestConfig(
            symbol="SPY",
            mode="agent",
            cadence="daily",
            data=_frame(),
            starting_cash=1000.0,
            commission_usd=0.0,
            slippage_bps=0.0,
            persist=False,
            bootstrap_iterations=0,
            options_enabled=False,
        ),
        pipeline=fake_pipeline,
    )

    assert len(seen) == len(_frame())
    assert result.equity_curve[1]["position_qty"] > 0
    assert all(point["option_position_count"] == 0 for point in result.equity_curve)


def _config(
    *,
    starting_cash: float = 10_000.0,
    bt_id: str | None = None,
) -> BacktestConfig:
    return BacktestConfig(
        symbol="SPY",
        mode="agent",
        cadence="daily",
        data=_option_frame(),
        starting_cash=starting_cash,
        commission_usd=0.0,
        slippage_bps=0.0,
        persist=False,
        bootstrap_iterations=0,
        bt_id=bt_id,
        options_enabled=True,
        options_universe=["SPY"],
    )


def _atm_call(chain: OptionChainSnapshot) -> OptionQuote:
    calls = [quote for quote in chain.quotes if quote.contract.kind == "call"]
    return min(calls, key=lambda quote: abs(quote.contract.strike - chain.spot))


def _option_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=8)
    closes = [100.0, 100.5, 101.0, 100.75, 101.25, 101.5, 101.75, 102.0]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 1.0 for close in closes],
            "low": [close - 1.0 for close in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=dates,
    )


def _frame() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=6)
    closes = [10.0, 11.0, 12.0, 13.0, 12.0, 14.0]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 1.0 for close in closes],
            "low": [close - 1.0 for close in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=dates,
    )

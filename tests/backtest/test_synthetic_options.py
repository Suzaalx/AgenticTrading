from __future__ import annotations

import pandas as pd
import pytest

from sentinel.backtest.engine import BacktestConfig, run_backtest
from sentinel.backtest.synthetic_chain import SyntheticChainProvider
from sentinel.options.pricing import bsm_price, crr_price


def test_synthetic_option_backtest_is_deterministic() -> None:
    frame = _frame(90)
    config = BacktestConfig(
        symbol="SPY",
        mode="rule",
        strategy="csp_wheel",
        data=frame,
        benchmark_data=frame,
        starting_cash=20_000.0,
        commission_usd=0.0,
        slippage_bps=0.0,
        persist=False,
        bootstrap_iterations=0,
    )

    first = run_backtest(config)
    second = run_backtest(config)

    assert first.trades == second.trades
    assert first.equity_curve == second.equity_curve
    assert first.premium_captured == second.premium_captured


def test_synthetic_chain_does_not_see_future_spike() -> None:
    base = _frame(45)
    spiked = base.copy()
    spiked.iloc[-1, spiked.columns.get_loc("high")] = 1_000.0
    spiked.iloc[-1, spiked.columns.get_loc("close")] = 900.0
    as_of = base.index[25]

    first = SyntheticChainProvider(base, symbol="SPY").chain_for(as_of)
    second = SyntheticChainProvider(spiked, symbol="SPY").chain_for(as_of)

    assert first.spot == second.spot
    assert first.rv_yang_zhang == second.rv_yang_zhang
    assert [(q.contract.contract_symbol, q.bid, q.ask) for q in first.quotes] == [
        (q.contract.contract_symbol, q.bid, q.ask) for q in second.quotes
    ]


def test_covered_call_premium_capture_matches_synthetic_bid() -> None:
    frame = _frame(50)
    result = run_backtest(
        BacktestConfig(
            symbol="SPY",
            mode="rule",
            strategy="covered_call_overwrite",
            data=frame,
            benchmark_data=frame,
            starting_cash=20_000.0,
            commission_usd=0.0,
            slippage_bps=0.0,
            persist=False,
            bootstrap_iterations=0,
        )
    )

    first_trade = result.trades[0]
    expected = float(first_trade["entry_price"]) * 100.0
    assert first_trade["premium_captured"] == pytest.approx(expected)
    assert result.premium_captured >= expected
    assert first_trade["pricing_source"] == "synthetic_bsm"


def test_crr_matches_bsm_for_synthetic_european_contract() -> None:
    frame = _frame(30)
    chain = SyntheticChainProvider(frame, symbol="SPY").chain_for(frame.index[-1])
    quote = min(
        (q for q in chain.quotes if q.contract.kind == "call"),
        key=lambda q: abs(float(q.contract.strike) - float(chain.spot)),
    )
    t_exp = (quote.contract.expiry - chain.as_of.date()).days / 365.0
    sigma = quote.model_iv or 0.20

    bsm = bsm_price(float(chain.spot), float(quote.contract.strike), t_exp, chain.risk_free_rate, 0.0, sigma, "call")
    crr = crr_price(
        float(chain.spot),
        float(quote.contract.strike),
        t_exp,
        chain.risk_free_rate,
        0.0,
        sigma,
        "call",
        steps=400,
        american=False,
    )

    assert crr == pytest.approx(bsm, rel=2e-3, abs=0.03)


def _frame(periods: int) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=periods)
    closes = [100.0 + (idx * 0.12) + (0.8 if idx % 9 < 5 else -0.4) for idx in range(periods)]
    opens = [closes[0], *closes[:-1]]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_, close) + 1.0 for open_, close in zip(opens, closes, strict=True)],
            "low": [min(open_, close) - 1.0 for open_, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1_000_000] * periods,
        },
        index=dates,
    )

from __future__ import annotations

import pandas as pd

from sentinel.backtest.engine import BacktestConfig, run_backtest


def test_sma_cross_is_deterministic_on_fixed_frame() -> None:
    frame = _spy_like_frame()
    config = BacktestConfig(
        symbol="SPY",
        mode="rule",
        strategy="sma_cross",
        strategy_params={"short_window": 3, "long_window": 8},
        data=frame,
        benchmark_data=frame,
        benchmark_symbol="SPY",
        starting_cash=10_000.0,
        commission_usd=0.0,
        slippage_bps=0.0,
        persist=False,
        bootstrap_iterations=200,
        bootstrap_seed=7,
    )

    first = run_backtest(config)
    second = run_backtest(config)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def _spy_like_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=80)
    closes = [100 + (idx * 0.2) + (2.0 if idx % 15 < 8 else -1.5) for idx in range(80)]
    opens = [closes[0], *closes[:-1]]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_, close) + 1.0 for open_, close in zip(opens, closes, strict=True)],
            "low": [min(open_, close) - 1.0 for open_, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1_000_000 + idx for idx in range(80)],
        },
        index=dates,
    )

from __future__ import annotations

import pandas as pd

from sentinel.backtest.engine import BacktestConfig, run_backtest
from sentinel.backtest.strategies.base import BarContext
from sentinel.core.models import Signal


class SpikeCheatStrategy:
    def on_bar(self, context: BarContext) -> Signal:
        if float(context.bar["close"]) > 100.0:
            return Signal(action="BUY", size=1.0)
        return Signal(action="HOLD", size=None)


def test_decision_on_spike_executes_next_open_not_current_bar() -> None:
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.0],
            "high": [10.0, 1000.0, 10.0],
            "low": [10.0, 10.0, 10.0],
            "close": [10.0, 1000.0, 10.0],
            "volume": [1000, 1000, 1000],
        },
        index=pd.bdate_range("2025-01-01", periods=3),
    )

    result = run_backtest(
        BacktestConfig(
            symbol="SPY",
            mode="rule",
            strategy=SpikeCheatStrategy(),
            data=frame,
            starting_cash=1000.0,
            commission_usd=0.0,
            slippage_bps=0.0,
            persist=False,
            bootstrap_iterations=0,
        )
    )

    assert result.total_return == 0.0
    assert result.equity_curve[1]["equity"] == 1000.0
    assert result.equity_curve[2]["equity"] == 1000.0

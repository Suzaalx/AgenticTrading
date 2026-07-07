from __future__ import annotations

import pandas as pd

from sentinel.backtest.strategies import BuyHoldStrategy, RSIMeanRevertStrategy, SMACrossStrategy
from sentinel.backtest.strategies.base import BarContext


def _context(frame: pd.DataFrame, *, position_qty: float = 0.0) -> BarContext:
    row = frame.iloc[-1]
    return BarContext(
        symbol="SPY",
        timestamp=pd.Timestamp(frame.index[-1]),
        bar=row,
        history=frame,
        cash=1000.0,
        position_qty=position_qty,
        position_value=position_qty * float(row["close"]),
        equity=1000.0,
    )


def test_sma_cross_emits_buy_on_cross_above() -> None:
    frame = pd.DataFrame(
        {
            "open": [3.0, 2.0, 1.0, 4.0],
            "high": [3.0, 2.0, 1.0, 4.0],
            "low": [3.0, 2.0, 1.0, 4.0],
            "close": [3.0, 2.0, 1.0, 4.0],
            "volume": [100] * 4,
        },
        index=pd.bdate_range("2025-01-01", periods=4),
    )
    assert SMACrossStrategy(short_window=2, long_window=3).on_bar(_context(frame)).action == "BUY"


def test_rsi_meanrevert_emits_buy_and_sell() -> None:
    oversold = pd.DataFrame({"close": [10.0], "rsi_14": [25.0]}, index=[pd.Timestamp("2025-01-01")])
    overbought = pd.DataFrame({"close": [10.0], "rsi_14": [75.0]}, index=[pd.Timestamp("2025-01-02")])

    strategy = RSIMeanRevertStrategy(lower=30, upper=70)

    assert strategy.on_bar(_context(oversold)).action == "BUY"
    assert strategy.on_bar(_context(overbought, position_qty=1.0)).action == "SELL"


def test_buy_hold_buys_once_then_holds() -> None:
    frame = pd.DataFrame({"close": [10.0]}, index=[pd.Timestamp("2025-01-01")])
    strategy = BuyHoldStrategy()

    assert strategy.on_bar(_context(frame, position_qty=0.0)).action == "BUY"
    assert strategy.on_bar(_context(frame, position_qty=1.0)).action == "HOLD"

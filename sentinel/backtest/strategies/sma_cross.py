"""Simple moving-average crossover baseline strategy."""

from __future__ import annotations

import math
from typing import cast

import pandas as pd

from sentinel.backtest.strategies.base import BarContext
from sentinel.core.models import Signal


class SMACrossStrategy:
    """Buy when the short SMA crosses above the long SMA; sell on the reverse cross."""

    def __init__(self, short_window: int = 20, long_window: int = 50, size: float = 1.0) -> None:
        if short_window <= 0 or long_window <= 0 or short_window >= long_window:
            msg = "expected 0 < short_window < long_window"
            raise ValueError(msg)
        self.short_window = short_window
        self.long_window = long_window
        self.size = size

    def on_bar(self, context: BarContext) -> Signal:
        values = _sma_values(context.history, self.short_window, self.long_window)
        if values is None:
            return Signal(action="HOLD", size=None)
        short_now, long_now, short_prev, long_prev = values
        if short_now > long_now and short_prev <= long_prev:
            return Signal(action="BUY", size=self.size)
        if short_now < long_now and short_prev >= long_prev:
            return Signal(action="SELL", size=1.0)
        return Signal(action="HOLD", size=None)


def _sma_values(
    history: pd.DataFrame, short_window: int, long_window: int
) -> tuple[float, float, float, float] | None:
    if len(history) < long_window + 1:
        return None
    short_col = f"sma_{short_window}"
    long_col = f"sma_{long_window}"
    if short_col in history.columns and long_col in history.columns:
        short = cast(pd.Series, pd.to_numeric(history[short_col], errors="coerce"))
        long = cast(pd.Series, pd.to_numeric(history[long_col], errors="coerce"))
    else:
        close = cast(pd.Series, pd.to_numeric(history["close"], errors="coerce"))
        short = cast(pd.Series, close.rolling(short_window, min_periods=short_window).mean())
        long = cast(pd.Series, close.rolling(long_window, min_periods=long_window).mean())
    values = (float(short.iloc[-1]), float(long.iloc[-1]), float(short.iloc[-2]), float(long.iloc[-2]))
    if not all(math.isfinite(value) for value in values):
        return None
    return values

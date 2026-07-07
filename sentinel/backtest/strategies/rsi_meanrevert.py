"""RSI mean-reversion baseline strategy."""

from __future__ import annotations

import math
from typing import cast

import pandas as pd

from sentinel.backtest.strategies.base import BarContext
from sentinel.core.models import Signal


class RSIMeanRevertStrategy:
    """Buy oversold RSI and sell overbought RSI."""

    def __init__(self, lower: float = 30.0, upper: float = 70.0, size: float = 1.0) -> None:
        self.lower = lower
        self.upper = upper
        self.size = size

    def on_bar(self, context: BarContext) -> Signal:
        rsi = _latest_rsi(context.history)
        if rsi is None:
            return Signal(action="HOLD", size=None)
        if rsi < self.lower:
            return Signal(action="BUY", size=self.size)
        if rsi > self.upper:
            return Signal(action="SELL", size=1.0)
        return Signal(action="HOLD", size=None)


def _latest_rsi(history: pd.DataFrame) -> float | None:
    if "rsi_14" in history.columns:
        value = float(history["rsi_14"].iloc[-1])
        return value if math.isfinite(value) else None
    close = cast(pd.Series, pd.to_numeric(history["close"], errors="coerce"))
    if len(close) < 15:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).tail(14).mean()
    loss = -delta.clip(upper=0).tail(14).mean()
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    rs = gain / loss
    return float(100 - (100 / (1 + rs)))

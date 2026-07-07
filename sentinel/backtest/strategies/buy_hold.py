"""Buy-and-hold baseline strategy."""

from __future__ import annotations

from sentinel.backtest.strategies.base import BarContext
from sentinel.core.models import Signal


class BuyHoldStrategy:
    """Buy once, then hold the position."""

    def __init__(self, size: float = 1.0) -> None:
        self.size = size

    def on_bar(self, context: BarContext) -> Signal:
        if context.position_qty <= 0:
            return Signal(action="BUY", size=self.size)
        return Signal(action="HOLD", size=None)

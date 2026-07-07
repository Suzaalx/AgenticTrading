"""Strategy protocol and bar context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from sentinel.core.models import Signal


@dataclass(frozen=True)
class BarContext:
    """Read-only point-in-time data passed to rule strategies."""

    symbol: str
    timestamp: pd.Timestamp
    bar: pd.Series
    history: pd.DataFrame
    cash: float
    position_qty: float
    position_value: float
    equity: float


class Strategy(Protocol):
    """Deterministic rule strategy interface."""

    def on_bar(self, context: BarContext) -> Signal:
        """Return the signal computed from data available through ``context.timestamp``."""
        ...

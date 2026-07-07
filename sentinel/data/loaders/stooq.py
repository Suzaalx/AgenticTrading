"""Stooq market data loader via pandas-datareader."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pandas as pd

from sentinel.core.models import Quote

from .base import normalize_ohlcv


class StooqLoader:
    """Load daily OHLCV bars from Stooq."""

    name = "stooq"

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        if interval != "1d":
            msg = "Stooq supports daily bars only"
            raise ValueError(msg)
        from pandas_datareader import data as pdr

        frame = cast(pd.DataFrame, pdr.DataReader(_stooq_symbol(symbol), "stooq", start, end))
        return normalize_ohlcv(frame)

    def get_quote(self, symbol: str) -> Quote:
        today = datetime.now(UTC).date()
        frame = self.get_ohlcv(symbol, today - timedelta(days=10), today)
        if frame.empty:
            msg = f"stooq quote unavailable for {symbol}"
            raise RuntimeError(msg)
        return Quote(
            symbol=symbol.upper(),
            price=Decimal(str(float(frame["close"].iloc[-1]))),
            ts=datetime.now(UTC),
            source=self.name,
        )


def _stooq_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "-USD" in normalized or "." in normalized:
        return normalized
    return f"{normalized}.US"

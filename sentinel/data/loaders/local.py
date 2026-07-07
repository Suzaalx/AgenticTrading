"""Local CSV/Parquet loader with deterministic offline fallback data."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from sentinel.core.models import FundamentalsSnapshot, NewsItem, Quote
from sentinel.store.db import sentinel_home

from .base import empty_fundamentals, empty_ohlcv, normalize_ohlcv


class LocalDataLoader:
    """Load OHLCV from local files, or synthesize deterministic bars offline."""

    name = "local"

    def __init__(self, root: Path | None = None, *, allow_synthetic: bool = True) -> None:
        self.root = root or (sentinel_home() / "cache" / "local")
        self.allow_synthetic = allow_synthetic

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        path = self._find_file(symbol, interval)
        if path is not None:
            frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, index_col=0)
            return self._window(normalize_ohlcv(frame), start, end)
        if not self.allow_synthetic:
            return empty_ohlcv()
        return self._synthetic_ohlcv(symbol, start, end)

    def get_quote(self, symbol: str) -> Quote:
        today = datetime.now(UTC).date()
        frame = self.get_ohlcv(symbol, today - timedelta(days=10), today)
        price = Decimal("0")
        if not frame.empty:
            price = Decimal(str(round(float(frame["close"].iloc[-1]), 4)))
        return Quote(symbol=symbol.upper(), price=price, ts=datetime.now(UTC), source=self.name)

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        _ = (symbol, lookback_days, limit)
        return []

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot:
        return empty_fundamentals(symbol, datetime.now(UTC).date())

    def _find_file(self, symbol: str, interval: str) -> Path | None:
        safe_symbol = symbol.upper().replace("/", "-")
        candidates = (
            self.root / f"{safe_symbol}_{interval}.parquet",
            self.root / f"{safe_symbol}_{interval}.csv",
            self.root / f"{safe_symbol}.parquet",
            self.root / f"{safe_symbol}.csv",
        )
        return next((path for path in candidates if path.exists()), None)

    @staticmethod
    def _window(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        if frame.empty:
            return frame
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        return frame.loc[(frame.index >= start_ts) & (frame.index <= end_ts)]

    @staticmethod
    def _synthetic_ohlcv(symbol: str, start: date, end: date) -> pd.DataFrame:
        if start > end:
            return empty_ohlcv()
        index = pd.bdate_range(start=start, end=end, name="date")
        if index.empty:
            index = pd.DatetimeIndex([pd.Timestamp(end)], name="date")
        seed = sum(ord(char) for char in symbol.upper())
        base = 50.0 + (seed % 150)
        steps = np.arange(len(index), dtype=float)
        trend = steps * 0.12
        seasonal = np.sin(steps / 5.0) * 1.5
        close = base + trend + seasonal
        open_ = close + np.cos(steps / 7.0) * 0.5
        high = np.maximum(open_, close) + 1.0
        low = np.minimum(open_, close) - 1.0
        volume = 1_000_000 + (steps.astype(int) % 20) * 10_000
        return pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            index=index,
        )

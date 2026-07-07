from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sentinel.data.router import DataRouter


class YFinanceFake:
    name = "yfinance"

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        _ = (symbol, start, end, interval)
        if self.mode == "raise":
            raise RuntimeError("provider down")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class StooqFake:
    name = "stooq"

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        _ = (symbol, start, end, interval)
        return pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [12.0, 13.0],
                "low": [9.0, 10.0],
                "close": [11.0, 12.0],
                "volume": [1000, 1100],
            },
            index=pd.date_range("2025-01-01", periods=2, name="date"),
        )


@pytest.mark.parametrize("mode", ["raise", "empty"])
def test_router_falls_back_to_stooq_and_records_provider(mode: str) -> None:
    router = DataRouter(loaders=[YFinanceFake(mode), StooqFake()])

    frame = router.get_ohlcv("NVDA", date(2025, 1, 1), date(2025, 1, 2))

    assert not frame.empty
    assert router.providers_used["ohlcv"] == "stooq"
    assert router.providers_used["ohlcv:NVDA"] == "stooq"

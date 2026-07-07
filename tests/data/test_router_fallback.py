from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from sentinel.core.models import NewsItem
from sentinel.data.cache import DiskCache
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


class CountingYFinanceFake(YFinanceFake):
    def __init__(self) -> None:
        super().__init__("ok")
        self.calls = 0

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        self.calls += 1
        _ = (symbol, start, end, interval)
        return pd.DataFrame(
            {
                "open": [20.0],
                "high": [21.0],
                "low": [19.0],
                "close": [20.5],
                "volume": [2000],
            },
            index=pd.date_range("2025-02-01", periods=1, name="date"),
        )


class EmptyNewsFake:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        self.calls += 1
        _ = (symbol, lookback_days, limit)
        return []


class YFinanceNewsFake(EmptyNewsFake):
    def __init__(self) -> None:
        super().__init__("yfinance_news")


class YFinanceNewsFallbackFake(EmptyNewsFake):
    def __init__(self) -> None:
        super().__init__("yfinance")

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        self.calls += 1
        _ = (lookback_days, limit)
        return [
            NewsItem(
                title="Plain yfinance should not be used",
                summary="fallback",
                source="yfinance",
                url="https://example.invalid/news",
                published=datetime(2026, 1, 1, tzinfo=UTC),
                symbols=[symbol],
            )
        ]


@pytest.mark.parametrize("mode", ["raise", "empty"])
def test_router_falls_back_to_stooq_and_records_provider(mode: str) -> None:
    router = DataRouter(loaders=[YFinanceFake(mode), StooqFake()])

    frame = router.get_ohlcv("NVDA", date(2025, 1, 1), date(2025, 1, 2))

    assert not frame.empty
    assert router.providers_used["ohlcv"] == "stooq"
    assert router.providers_used["ohlcv:NVDA"] == "stooq"


def test_router_reuses_cached_ohlcv(tmp_path) -> None:
    loader = CountingYFinanceFake()
    router = DataRouter(loaders=[loader], cache=DiskCache(tmp_path))
    start = date(2025, 2, 1)
    end = date(2025, 2, 3)

    first = router.get_ohlcv("NVDA", start, end)
    second = router.get_ohlcv("NVDA", start, end)

    assert loader.calls == 1
    assert float(first.iloc[0]["close"]) == 20.5
    assert float(second.iloc[0]["close"]) == 20.5
    assert router.providers_used["ohlcv:NVDA"] == "yfinance"


def test_news_chain_omits_plain_yfinance_fallback() -> None:
    finnhub = EmptyNewsFake("finnhub")
    yfinance_news = YFinanceNewsFake()
    plain_yfinance = YFinanceNewsFallbackFake()
    router = DataRouter(loaders=[finnhub, yfinance_news, plain_yfinance])

    news = router.get_news("NVDA", lookback_days=3, limit=5)

    assert news == []
    assert finnhub.calls == 1
    assert yfinance_news.calls == 1
    assert plain_yfinance.calls == 0
    assert router.providers_used["news:NVDA"] == "none"

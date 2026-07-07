"""Public loader interfaces and concrete provider implementations."""

from sentinel.data.loaders.alphavantage import AlphaVantageLoader
from sentinel.data.loaders.base import (
    FundamentalsLoader,
    MarketDataLoader,
    NewsLoader,
    empty_ohlcv,
    normalize_ohlcv,
)
from sentinel.data.loaders.finnhub import FinnhubLoader
from sentinel.data.loaders.local import LocalDataLoader
from sentinel.data.loaders.stooq import StooqLoader
from sentinel.data.loaders.yfinance import YFinanceLoader, YFinanceNewsLoader

__all__ = [
    "AlphaVantageLoader",
    "FinnhubLoader",
    "FundamentalsLoader",
    "LocalDataLoader",
    "MarketDataLoader",
    "NewsLoader",
    "StooqLoader",
    "YFinanceLoader",
    "YFinanceNewsLoader",
    "empty_ohlcv",
    "normalize_ohlcv",
]

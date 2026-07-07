"""Public loader interfaces and concrete provider implementations."""

from sentinel.data.loaders.alphavantage import AlphaVantageLoader
from sentinel.data.loaders.base import (
    FundamentalsLoader,
    MarketDataLoader,
    NewsLoader,
    ProviderRateLimited,
    empty_ohlcv,
    normalize_ohlcv,
)
from sentinel.data.loaders.finnhub import FinnhubLoader
from sentinel.data.loaders.local import LocalDataLoader
from sentinel.data.loaders.rates import RateResult, dividend_yield, risk_free_rate
from sentinel.data.loaders.stooq import StooqLoader
from sentinel.data.loaders.yfinance import YFinanceLoader, YFinanceNewsLoader
from sentinel.data.loaders.yfinance_options import YFinanceOptionsLoader

__all__ = [
    "AlphaVantageLoader",
    "FinnhubLoader",
    "FundamentalsLoader",
    "LocalDataLoader",
    "MarketDataLoader",
    "NewsLoader",
    "ProviderRateLimited",
    "RateResult",
    "StooqLoader",
    "YFinanceLoader",
    "YFinanceNewsLoader",
    "YFinanceOptionsLoader",
    "dividend_yield",
    "empty_ohlcv",
    "normalize_ohlcv",
    "risk_free_rate",
]

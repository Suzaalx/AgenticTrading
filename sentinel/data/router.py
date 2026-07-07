"""Fallback data router for provider chains."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

import pandas as pd

from sentinel.config.settings import Settings, load_settings
from sentinel.core.models import FundamentalsSnapshot, NewsItem, Quote
from sentinel.data.cache import DiskCache
from sentinel.data.loaders import (
    AlphaVantageLoader,
    FinnhubLoader,
    LocalDataLoader,
    StooqLoader,
    YFinanceLoader,
    YFinanceNewsLoader,
    empty_ohlcv,
)
from sentinel.data.sanity import clean_ohlcv

logger = logging.getLogger(__name__)


class DataRouter:
    """Resolve data requests against ordered Sentinel fallback chains."""

    def __init__(
        self,
        loaders: Sequence[object] | None = None,
        *,
        settings: Settings | None = None,
        cache: DiskCache | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.loaders = list(loaders) if loaders is not None else _default_loaders(self.settings)
        self.cache = cache if cache is not None else (DiskCache() if loaders is None else None)
        self.providers_used: dict[str, str] = {}
        self.notes: list[str] = []

    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        *,
        asset_class: str | None = None,
    ) -> pd.DataFrame:
        """Return OHLCV using equity or crypto fallback chains."""

        chain = ["yfinance", "local"] if _is_crypto(symbol, asset_class) else [
            "yfinance",
            "stooq",
            "alphavantage",
            "local",
        ]
        for loader in self._chain(chain):
            provider = cast(str, getattr(loader, "name", "unknown"))
            cached = self._cached_ohlcv(provider, symbol, start, end, interval)
            if cached is not None:
                self._record("ohlcv", symbol, provider)
                return cached
            try:
                raw = cast(Any, loader).get_ohlcv(symbol, start, end, interval)
                if raw is None or raw.empty:
                    continue
                clean = clean_ohlcv(raw)
                if clean.empty:
                    continue
                self._store_ohlcv(provider, symbol, start, end, interval, clean)
                self._record("ohlcv", symbol, provider)
                return clean
            except Exception as exc:
                logger.info("%s OHLCV failed for %s: %s", provider, symbol, exc)
        self._record("ohlcv", symbol, "none")
        self.notes.append(f"OHLCV data unavailable for {symbol}")
        return empty_ohlcv()

    def get_quote(self, symbol: str) -> Quote | None:
        """Return latest quote, degrading to ``None`` when all providers fail."""

        for loader in self._chain(["yfinance", "stooq", "alphavantage", "local"]):
            try:
                quote = cast(Any, loader).get_quote(symbol)
                self._record("quote", symbol, cast(str, getattr(loader, "name", "unknown")))
                return quote
            except Exception as exc:
                logger.info("%s quote failed for %s: %s", getattr(loader, "name", "unknown"), symbol, exc)
        self._record("quote", symbol, "none")
        self.notes.append(f"quote data unavailable for {symbol}")
        return None

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        """Return news from Finnhub, yfinance news, or an empty list."""

        for loader in self._chain(["finnhub", "yfinance_news", "yfinance"]):
            try:
                news = cast(Any, loader).get_news(symbol, lookback_days, limit)
                if not news:
                    continue
                self._record("news", symbol, cast(str, getattr(loader, "name", "unknown")))
                return list(news)[:limit]
            except Exception as exc:
                logger.info("%s news failed for %s: %s", getattr(loader, "name", "unknown"), symbol, exc)
        self._record("news", symbol, "none")
        self.notes.append(f"news data unavailable for {symbol}")
        return []

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot | None:
        """Return fundamentals from yfinance, Alpha Vantage, or ``None``."""

        for loader in self._chain(["yfinance", "alphavantage"]):
            try:
                fundamentals = cast(Any, loader).get_fundamentals(symbol)
                if fundamentals is None:
                    continue
                self._record("fundamentals", symbol, cast(str, getattr(loader, "name", "unknown")))
                return fundamentals
            except Exception as exc:
                logger.info(
                    "%s fundamentals failed for %s: %s",
                    getattr(loader, "name", "unknown"),
                    symbol,
                    exc,
                )
        self._record("fundamentals", symbol, "none")
        self.notes.append(f"fundamentals data unavailable for {symbol}")
        return None

    def _chain(self, names: Sequence[str]) -> list[object]:
        by_name = {str(getattr(loader, "name", "")): loader for loader in self.loaders}
        return [by_name[name] for name in names if name in by_name]

    def _record(self, kind: str, symbol: str, provider: str) -> None:
        self.providers_used[kind] = provider
        self.providers_used[f"{kind}:{symbol.upper()}"] = provider

    def _cached_ohlcv(
        self,
        provider: str,
        symbol: str,
        start: date,
        end: date,
        interval: str,
    ) -> pd.DataFrame | None:
        if self.cache is None:
            return None
        try:
            cached = self.cache.get_frame(provider, symbol, interval, start, end)
        except Exception as exc:
            logger.info("%s cache read failed for %s: %s", provider, symbol, exc)
            return None
        if cached is None or cached.empty:
            return None
        clean = clean_ohlcv(cached)
        return None if clean.empty else clean

    def _store_ohlcv(
        self,
        provider: str,
        symbol: str,
        start: date,
        end: date,
        interval: str,
        frame: pd.DataFrame,
    ) -> None:
        if self.cache is None:
            return
        try:
            self.cache.set_frame(provider, symbol, interval, start, end, frame)
        except Exception as exc:
            logger.info("%s cache write failed for %s: %s", provider, symbol, exc)


def _default_loaders(settings: Settings) -> list[object]:
    loaders: list[object] = [YFinanceLoader(), StooqLoader()]
    if os.environ.get(settings.data.alpha_vantage_key_env):
        loaders.append(AlphaVantageLoader(key_env=settings.data.alpha_vantage_key_env))
    if os.environ.get(settings.data.finnhub_key_env):
        loaders.append(FinnhubLoader(key_env=settings.data.finnhub_key_env))
    loaders.extend([YFinanceNewsLoader(), LocalDataLoader()])
    return loaders


def _is_crypto(symbol: str, asset_class: str | None) -> bool:
    if asset_class:
        return asset_class.lower() == "crypto"
    upper = symbol.upper()
    return upper.endswith("-USD") or (upper.endswith("USD") and upper[:3] in {"BTC", "ETH", "SOL"})

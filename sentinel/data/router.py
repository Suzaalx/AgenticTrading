"""Fallback data router for provider chains."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pandas as pd

from sentinel.config.settings import Settings, load_mandate, load_settings
from sentinel.core.models import (
    FundamentalsSnapshot,
    MandateOptions,
    NewsItem,
    OptionChainSnapshot,
    Quote,
)
from sentinel.data.cache import DiskCache
from sentinel.data.loaders import (
    AlphaVantageLoader,
    FinnhubLoader,
    LocalDataLoader,
    ProviderRateLimited,
    StooqLoader,
    YFinanceLoader,
    YFinanceNewsLoader,
    YFinanceOptionsLoader,
    empty_ohlcv,
)
from sentinel.data.loaders.rates import dividend_yield, risk_free_rate
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
            record_provider = _ohlcv_provider_label(loader, provider, symbol, interval)
            if cached is not None:
                self._record("ohlcv", symbol, record_provider)
                return cached
            try:
                raw = cast(Any, loader).get_ohlcv(symbol, start, end, interval)
                if raw is None or raw.empty:
                    continue
                clean = clean_ohlcv(raw)
                if clean.empty:
                    continue
                self._store_ohlcv(provider, symbol, start, end, interval, clean)
                self._record("ohlcv", symbol, record_provider)
                return clean
            except ProviderRateLimited as exc:
                _record_rate_limit(self.notes, provider, symbol, exc)
                logger.warning("%s rate-limited for %s; falling back", provider, symbol)
            except Exception as exc:
                logger.info("%s OHLCV failed for %s: %s", provider, symbol, exc)
        self._record("ohlcv", symbol, "none")
        self.notes.append(f"OHLCV data unavailable for {symbol}")
        return empty_ohlcv()

    def get_quote(self, symbol: str) -> Quote | None:
        """Return latest quote, degrading to ``None`` when all providers fail."""

        for loader in self._chain(["yfinance", "stooq", "alphavantage", "local"]):
            provider = cast(str, getattr(loader, "name", "unknown"))
            try:
                quote = cast(Any, loader).get_quote(symbol)
                self._record("quote", symbol, provider)
                return quote
            except ProviderRateLimited as exc:
                _record_rate_limit(self.notes, provider, symbol, exc)
                logger.warning("%s rate-limited for %s; falling back", provider, symbol)
            except Exception as exc:
                logger.info("%s quote failed for %s: %s", provider, symbol, exc)
        self._record("quote", symbol, "none")
        self.notes.append(f"quote data unavailable for {symbol}")
        return None

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        """Return news from Finnhub, yfinance news, or an empty list."""

        for loader in self._chain(["finnhub", "yfinance_news"]):
            try:
                news = cast(Any, loader).get_news(symbol, lookback_days, limit)
                if not news:
                    continue
                self._record("news", symbol, cast(str, getattr(loader, "name", "unknown")))
                return list(news)[:limit]
            except ProviderRateLimited as exc:
                provider = cast(str, getattr(loader, "name", "unknown"))
                _record_rate_limit(self.notes, provider, symbol, exc)
                logger.warning("%s rate-limited for %s; falling back", provider, symbol)
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
            except ProviderRateLimited as exc:
                provider = cast(str, getattr(loader, "name", "unknown"))
                _record_rate_limit(self.notes, provider, symbol, exc)
                logger.warning("%s rate-limited for %s; falling back", provider, symbol)
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

    def get_option_chain(self, underlying: str, as_of: datetime) -> OptionChainSnapshot | None:
        """Return a live near-the-money option chain, or None when unavailable."""

        quote = self.get_quote(underlying)
        if quote is None:
            self._record("option_chain", underlying, "none")
            self.notes.append(f"option chain unavailable for {underlying}: spot unavailable")
            return None
        try:
            spot = float(quote.price)
        except (TypeError, ValueError):
            self._record("option_chain", underlying, "none")
            self.notes.append(f"option chain unavailable for {underlying}: invalid spot")
            return None

        as_of_utc = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        rate = risk_free_rate(as_of_utc, settings=self.settings, cache=self.cache)
        if rate.note:
            self.notes.append(rate.note)
        fundamentals = self.get_fundamentals(underlying)
        div = dividend_yield(underlying, fundamentals=fundamentals)
        if div.note:
            self.notes.append(div.note)
        dte = _option_dte_window()

        for loader in self._chain(["yfinance_options"]):
            provider = cast(str, getattr(loader, "name", "unknown"))
            try:
                loaded = cast(Any, loader).load_option_chain(
                    underlying,
                    as_of_utc,
                    spot,
                    rate.value,
                    div.value,
                    min_dte=dte.min_dte,
                    max_dte=dte.max_dte,
                )
                if loaded is None or not loaded.quotes:
                    continue
                self._record("option_chain", underlying, provider)
                providers = dict(self.providers_used)
                providers["risk_free_rate"] = rate.provider
                providers["dividend_yield"] = div.provider
                return OptionChainSnapshot(
                    run_id="",
                    underlying=underlying.upper(),
                    as_of=as_of_utc,
                    spot=Decimal(str(spot)),
                    risk_free_rate=rate.value,
                    dividend_yield=div.value,
                    expiries=list(loaded.expiries),
                    quotes=list(loaded.quotes),
                    atm_iv=None,
                    iv_rank=None,
                    iv_percentile=None,
                    rv_yang_zhang=None,
                    pricing_source="live_chain",
                    providers_used=providers,
                )
            except ProviderRateLimited as exc:
                _record_rate_limit(self.notes, provider, underlying, exc)
                logger.warning("%s rate-limited for %s; falling back", provider, underlying)
            except Exception as exc:
                logger.info("%s option chain failed for %s: %s", provider, underlying, exc)
        self._record("option_chain", underlying, "none")
        self.notes.append(f"option chain unavailable for {underlying}")
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
    loaders: list[object] = [YFinanceLoader(), YFinanceOptionsLoader(), StooqLoader()]
    if os.environ.get(settings.data.alpha_vantage_key_env):
        loaders.append(AlphaVantageLoader(key_env=settings.data.alpha_vantage_key_env))
    if os.environ.get(settings.data.finnhub_key_env):
        loaders.append(FinnhubLoader(key_env=settings.data.finnhub_key_env))
    loaders.extend([YFinanceNewsLoader(), LocalDataLoader()])
    return loaders


def _record_rate_limit(
    notes: list[str],
    provider: str,
    symbol: str,
    exc: ProviderRateLimited,
) -> None:
    suffix = ""
    if exc.retry_after_seconds is not None:
        suffix = f" (retry after {exc.retry_after_seconds:g}s)"
    note = f"{provider} rate-limited for {symbol}; fell back to next provider{suffix}"
    if note not in notes:
        notes.append(note)


def _ohlcv_provider_label(loader: object, provider: str, symbol: str, interval: str) -> str:
    if provider != "local" or not bool(getattr(loader, "allow_synthetic", False)):
        return provider
    find_file = getattr(loader, "_find_file", None)
    if not callable(find_file):
        return "local_synthetic"
    return provider if find_file(symbol, interval) is not None else "local_synthetic"


def _is_crypto(symbol: str, asset_class: str | None) -> bool:
    if asset_class:
        return asset_class.lower() == "crypto"
    upper = symbol.upper()
    return upper.endswith("-USD") or (upper.endswith("USD") and upper[:3] in {"BTC", "ETH", "SOL"})


def _option_dte_window() -> MandateOptions:
    try:
        return load_mandate().options
    except Exception:
        return MandateOptions()

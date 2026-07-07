"""Loader protocols and normalization helpers for Sentinel market data."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol, cast, runtime_checkable

import pandas as pd

from sentinel.core.models import FundamentalsSnapshot, NewsItem, Quote

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


@runtime_checkable
class MarketDataLoader(Protocol):
    """Provider capable of loading normalized OHLCV bars and latest quotes."""

    name: str

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame: ...

    def get_quote(self, symbol: str) -> Quote: ...


@runtime_checkable
class NewsLoader(Protocol):
    """Provider capable of loading normalized news items."""

    name: str

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]: ...


@runtime_checkable
class FundamentalsLoader(Protocol):
    """Provider capable of loading normalized fundamentals."""

    name: str

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot: ...


def empty_ohlcv() -> pd.DataFrame:
    """Return an empty frame with Sentinel's canonical OHLCV schema."""

    return pd.DataFrame(columns=OHLCV_COLUMNS).rename_axis("date")


def _flatten_column(column: object) -> str:
    if isinstance(column, tuple):
        return " ".join(str(part) for part in column if part is not None)
    return str(column)


def normalize_ohlcv(
    df: pd.DataFrame,
    *,
    column_map: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Normalize provider-specific OHLCV frames to lower-case Sentinel columns."""

    if df is None or df.empty:
        return empty_ohlcv()

    working = df.copy()
    if isinstance(working.columns, pd.MultiIndex):
        working.columns = [_flatten_column(col) for col in working.columns.to_list()]

    lower_to_original = {
        str(column).strip().lower().replace("_", " "): column for column in working.columns
    }
    aliases: dict[str, tuple[str, ...]] = {
        "open": ("open",),
        "high": ("high",),
        "low": ("low",),
        "close": ("close", "adj close", "adjusted close", "4. close", "5. adjusted close"),
        "volume": ("volume", "6. volume"),
    }
    if column_map:
        for canonical, provider_column in column_map.items():
            aliases[canonical] = (provider_column.strip().lower().replace("_", " "),)

    normalized = pd.DataFrame(index=working.index)
    for canonical, candidates in aliases.items():
        source = next((lower_to_original[c] for c in candidates if c in lower_to_original), None)
        if source is None:
            return empty_ohlcv()
        normalized[canonical] = pd.to_numeric(working[source], errors="coerce")

    index = pd.DatetimeIndex(pd.to_datetime(normalized.index, errors="coerce"))
    normalized.index = cast(Any, index.tz_localize(None)).normalize()
    normalized = normalized.loc[~normalized.index.isna()]
    normalized = normalized[OHLCV_COLUMNS].dropna(subset=OHLCV_COLUMNS).sort_index()
    normalized.index.name = "date"
    return normalized


def empty_fundamentals(symbol: str, as_of: date) -> FundamentalsSnapshot:
    """Return a valid fundamentals snapshot with unavailable optional values."""

    return FundamentalsSnapshot(
        symbol=symbol.upper(),
        as_of=as_of,
        market_cap=None,
        pe_ttm=None,
        forward_pe=None,
        eps_ttm=None,
        revenue_growth_yoy=None,
        profit_margin=None,
        debt_to_equity=None,
        free_cash_flow=None,
        next_earnings_date=None,
        analyst_target_mean=None,
    )

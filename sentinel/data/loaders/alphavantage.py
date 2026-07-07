"""Alpha Vantage loader."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from sentinel.core.models import FundamentalsSnapshot, Quote

from .base import empty_fundamentals, normalize_ohlcv


class AlphaVantageLoader:
    """Load market data and fundamentals from Alpha Vantage when a key is present."""

    name = "alphavantage"

    def __init__(self, api_key: str | None = None, *, key_env: str = "ALPHA_VANTAGE_KEY") -> None:
        self.api_key = api_key or os.environ.get(key_env)
        self.base_url = "https://www.alphavantage.co/query"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        if not self.enabled:
            msg = "Alpha Vantage key is not configured"
            raise RuntimeError(msg)
        payload = self._get(
            {
                "function": "TIME_SERIES_DAILY_ADJUSTED" if interval == "1d" else "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "outputsize": "full",
                **({"interval": interval} if interval != "1d" else {}),
            }
        )
        series_key = "Time Series (Daily)" if interval == "1d" else f"Time Series ({interval})"
        series = payload.get(series_key)
        if not isinstance(series, dict):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame.from_dict(series, orient="index")
        normalized = normalize_ohlcv(
            frame,
            column_map={
                "open": "1. open",
                "high": "2. high",
                "low": "3. low",
                "close": "4. close",
                "volume": "6. volume" if interval == "1d" else "5. volume",
            },
        )
        return normalized.loc[(normalized.index >= pd.Timestamp(start)) & (normalized.index <= pd.Timestamp(end))]

    def get_quote(self, symbol: str) -> Quote:
        if not self.enabled:
            msg = "Alpha Vantage key is not configured"
            raise RuntimeError(msg)
        payload = self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
        quote = payload.get("Global Quote")
        if not isinstance(quote, dict) or not quote.get("05. price"):
            msg = f"Alpha Vantage quote unavailable for {symbol}"
            raise RuntimeError(msg)
        return Quote(
            symbol=symbol.upper(),
            price=Decimal(str(quote["05. price"])),
            ts=datetime.now(UTC),
            source=self.name,
        )

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot:
        if not self.enabled:
            msg = "Alpha Vantage key is not configured"
            raise RuntimeError(msg)
        payload = self._get({"function": "OVERVIEW", "symbol": symbol})
        if not payload:
            return empty_fundamentals(symbol, datetime.now(UTC).date())
        return FundamentalsSnapshot(
            symbol=symbol.upper(),
            as_of=datetime.now(UTC).date(),
            market_cap=_float_or_none(payload.get("MarketCapitalization")),
            pe_ttm=_float_or_none(payload.get("PERatio")),
            forward_pe=_float_or_none(payload.get("ForwardPE")),
            eps_ttm=_float_or_none(payload.get("EPS")),
            revenue_growth_yoy=_float_or_none(payload.get("QuarterlyRevenueGrowthYOY")),
            profit_margin=_float_or_none(payload.get("ProfitMargin")),
            debt_to_equity=_float_or_none(payload.get("DebtToEquityRatio")),
            free_cash_flow=None,
            next_earnings_date=None,
            analyst_target_mean=_float_or_none(payload.get("AnalystTargetPrice")),
        )

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        import httpx

        response = httpx.get(self.base_url, params={**params, "apikey": self.api_key}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return {}
        if "Error Message" in data or "Note" in data:
            msg = str(data.get("Error Message") or data.get("Note"))
            raise RuntimeError(msg)
        return data


def _float_or_none(value: object) -> float | None:
    try:
        if value is None or value in {"None", "-", ""}:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None

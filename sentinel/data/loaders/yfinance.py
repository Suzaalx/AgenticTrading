"""Yahoo Finance-backed loaders."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from sentinel.core.models import FundamentalsSnapshot, NewsItem, Quote

from .base import ProviderRateLimited, empty_fundamentals, normalize_ohlcv


class YFinanceLoader:
    """Load market data, news, and fundamentals from yfinance."""

    name = "yfinance"

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        import yfinance as yf

        try:
            ticker = yf.Ticker(symbol)
        except Exception as exc:
            if _is_yf_rate_limit(exc):
                raise ProviderRateLimited(self.name, symbol) from exc
            raise
        end_exclusive = end + timedelta(days=1)
        try:
            frame = ticker.history(
                start=start.isoformat(),
                end=end_exclusive.isoformat(),
                interval=interval,
                auto_adjust=False,
                timeout=8,
            )
        except Exception as exc:
            if _is_yf_rate_limit(exc):
                raise ProviderRateLimited(self.name, symbol) from exc
            raise
        return normalize_ohlcv(frame)

    def get_quote(self, symbol: str) -> Quote:
        import yfinance as yf

        try:
            ticker = yf.Ticker(symbol)
        except Exception as exc:
            if _is_yf_rate_limit(exc):
                raise ProviderRateLimited(self.name, symbol) from exc
            raise
        price: object | None = None
        try:
            fast_info = ticker.fast_info
            price = fast_info.get("last_price") if hasattr(fast_info, "get") else None
        except Exception as exc:
            if _is_yf_rate_limit(exc):
                raise ProviderRateLimited(self.name, symbol) from exc
            price = None
        if price is None:
            try:
                info = ticker.info
            except Exception as exc:
                if _is_yf_rate_limit(exc):
                    raise ProviderRateLimited(self.name, symbol) from exc
                raise
            price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price is None:
            today = datetime.now(UTC).date()
            frame = self.get_ohlcv(symbol, today - timedelta(days=10), today)
            if frame.empty:
                msg = f"yfinance quote unavailable for {symbol}"
                raise RuntimeError(msg)
            price = float(frame["close"].iloc[-1])
        return Quote(
            symbol=symbol.upper(),
            price=Decimal(str(price)),
            ts=datetime.now(UTC),
            source=self.name,
        )

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        import yfinance as yf

        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
        try:
            raw_items = yf.Ticker(symbol).news or []
        except Exception as exc:
            if _is_yf_rate_limit(exc):
                raise ProviderRateLimited(self.name, symbol) from exc
            raise
        items: list[NewsItem] = []
        for raw in raw_items[:limit]:
            item = self._parse_news_item(raw, symbol)
            if item is not None and item.published >= cutoff:
                items.append(item)
        return items[:limit]

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot:
        import yfinance as yf

        try:
            info: dict[str, Any] = yf.Ticker(symbol).info or {}
        except Exception as exc:
            if _is_yf_rate_limit(exc):
                raise ProviderRateLimited(self.name, symbol) from exc
            raise
        if not info:
            return empty_fundamentals(symbol, datetime.now(UTC).date())
        return FundamentalsSnapshot(
            symbol=symbol.upper(),
            as_of=datetime.now(UTC).date(),
            market_cap=_float_or_none(info.get("marketCap")),
            pe_ttm=_float_or_none(info.get("trailingPE")),
            forward_pe=_float_or_none(info.get("forwardPE")),
            eps_ttm=_float_or_none(info.get("trailingEps")),
            revenue_growth_yoy=_float_or_none(info.get("revenueGrowth")),
            profit_margin=_float_or_none(info.get("profitMargins")),
            debt_to_equity=_float_or_none(info.get("debtToEquity")),
            free_cash_flow=_float_or_none(info.get("freeCashflow")),
            next_earnings_date=None,
            analyst_target_mean=_float_or_none(info.get("targetMeanPrice")),
        )

    @staticmethod
    def _parse_news_item(raw: dict[str, Any], symbol: str) -> NewsItem | None:
        raw_content = raw.get("content")
        content = raw_content if isinstance(raw_content, dict) else raw
        title = str(content.get("title") or "").strip()
        if not title:
            return None
        published_raw = (
            content.get("pubDate")
            or content.get("displayTime")
            or content.get("providerPublishTime")
            or raw.get("providerPublishTime")
        )
        published = _parse_datetime(published_raw)
        summary = str(content.get("summary") or content.get("description") or "")
        provider = content.get("provider")
        canonical_url = content.get("canonicalUrl")
        source = str(
            (provider.get("displayName") if isinstance(provider, dict) else None)
            or content.get("publisher")
            or "yfinance"
        )
        url = str(
            (canonical_url.get("url") if isinstance(canonical_url, dict) else None)
            or content.get("link")
            or ""
        )
        return NewsItem(
            title=title,
            summary=summary,
            source=source,
            url=url,
            published=published,
            symbols=[symbol.upper()],
        )


class YFinanceNewsLoader(YFinanceLoader):
    """Yahoo Finance news provider recorded separately in router fallback chains."""

    name = "yfinance_news"


def _float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _is_yf_rate_limit(exc: BaseException) -> bool:
    try:
        from yfinance.exceptions import YFRateLimitError
    except Exception:
        YFRateLimitError = None  # type: ignore[assignment]
    if YFRateLimitError is not None and isinstance(exc, YFRateLimitError):
        return True
    class_name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return class_name == "yfratelimiterror" or (
        "rate limit" in message or "rate limited" in message or "too many requests" in message
    )


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.to_pydatetime()
    return datetime.now(UTC)

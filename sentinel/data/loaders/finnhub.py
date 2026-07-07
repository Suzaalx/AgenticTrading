"""Finnhub news loader."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinel.core.models import NewsItem


class FinnhubLoader:
    """Load company news from Finnhub when a key is present."""

    name = "finnhub"

    def __init__(self, api_key: str | None = None, *, key_env: str = "FINNHUB_KEY") -> None:
        self.api_key = api_key or os.environ.get(key_env)
        self.base_url = "https://finnhub.io/api/v1/company-news"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        if not self.enabled:
            msg = "Finnhub key is not configured"
            raise RuntimeError(msg)
        import httpx

        end = datetime.now(UTC).date()
        start = end - timedelta(days=lookback_days)
        response = httpx.get(
            self.base_url,
            params={
                "symbol": symbol,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": self.api_key,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [item for item in (_parse_item(raw, symbol) for raw in payload[:limit]) if item is not None]


def _parse_item(raw: Any, symbol: str) -> NewsItem | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("headline") or "").strip()
    if not title:
        return None
    published_raw = raw.get("datetime")
    published = (
        datetime.fromtimestamp(float(published_raw), tz=UTC)
        if isinstance(published_raw, int | float)
        else datetime.now(UTC)
    )
    return NewsItem(
        title=title,
        summary=str(raw.get("summary") or ""),
        source=str(raw.get("source") or "finnhub"),
        url=str(raw.get("url") or ""),
        published=published,
        symbols=[symbol.upper()],
    )

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pandas as pd

from sentinel.core.models import FundamentalsSnapshot, NewsItem
from sentinel.data.snapshot import build_snapshot, snapshot_sidecar_paths


class FakeRouter:
    def __init__(self) -> None:
        self.providers_used = {"ohlcv": "fixture", "news": "fixture", "fundamentals": "fixture"}

    def get_ohlcv(self, symbol: str, start: date, end: date, interval: str = "1d") -> pd.DataFrame:
        _ = (symbol, start, end, interval)
        return pd.DataFrame(
            {
                "open": [10.0] * 40,
                "high": [12.0] * 40,
                "low": [9.0] * 40,
                "close": [11.0 + i for i in range(40)],
                "volume": [1000 + i for i in range(40)],
            },
            index=pd.bdate_range("2025-01-01", periods=40, name="date"),
        )

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        _ = (lookback_days, limit)
        return [
            NewsItem(
                title="Fixture news",
                summary="summary",
                source="fixture",
                url="https://example.invalid/news",
                published=datetime(2025, 2, 1, tzinfo=UTC),
                symbols=[symbol],
            )
        ]

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot:
        return FundamentalsSnapshot(
            symbol=symbol,
            as_of=date(2025, 2, 1),
            market_cap=1.0,
            pe_ttm=2.0,
            forward_pe=None,
            eps_ttm=None,
            revenue_growth_yoy=None,
            profit_margin=None,
            debt_to_equity=None,
            free_cash_flow=None,
            next_earnings_date=None,
            analyst_target_mean=None,
        )


def test_build_snapshot_writes_parquet_and_json_sidecars() -> None:
    snapshot = build_snapshot(
        FakeRouter(),
        "NVDA",
        datetime(2025, 2, 1, 12, tzinfo=UTC),
        "RUN123",
    )
    sidecars = snapshot_sidecar_paths(snapshot)

    assert snapshot.symbol == "NVDA"
    assert snapshot.providers_used["ohlcv"] == "fixture"
    assert pd.read_parquet(snapshot.ohlcv_path).shape[0] == 40
    assert "sma_20" in pd.read_parquet(snapshot.indicators_path).columns
    assert sidecars["news"].exists()
    assert sidecars["fundamentals"].exists()
    assert json.loads(sidecars["news"].read_text(encoding="utf-8"))[0]["title"] == "Fixture news"
    assert json.loads(sidecars["fundamentals"].read_text(encoding="utf-8"))["symbol"] == "NVDA"

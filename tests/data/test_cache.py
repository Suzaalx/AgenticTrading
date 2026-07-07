from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd

from sentinel.data.cache import DiskCache, ttl_for


def test_frame_cache_hit_and_miss(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    start = date(2025, 1, 1)
    end = date(2025, 1, 31)
    frame = pd.DataFrame(
        {"open": [1], "high": [2], "low": [1], "close": [2], "volume": [100]},
        index=pd.date_range("2025-01-01", periods=1, name="date"),
    )

    assert cache.get_frame("fixture", "NVDA", "1d", start, end) is None
    cache.set_frame("fixture", "NVDA", "1d", start, end, frame)

    cached = cache.get_frame("fixture", "NVDA", "1d", start, end)
    assert cached is not None
    assert float(cached.iloc[0]["close"]) == 2.0


def test_cache_ttl_logic(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    now = datetime(2026, 7, 6, 15, tzinfo=UTC)
    start = date(2026, 7, 1)
    frame = pd.DataFrame(
        {"open": [1], "high": [2], "low": [1], "close": [2], "volume": [100]},
        index=pd.date_range("2026-07-01", periods=1, name="date"),
    )

    cache.set_frame("fixture", "NVDA", "5m", start, now.date(), frame, now=now - timedelta(minutes=16))
    assert cache.get_frame("fixture", "NVDA", "5m", start, now.date(), now=now) is None

    yesterday = now.date() - timedelta(days=1)
    cache.set_frame(
        "fixture",
        "NVDA",
        "1d",
        start,
        yesterday,
        frame,
        now=now - timedelta(hours=25),
    )
    assert cache.get_frame("fixture", "NVDA", "1d", start, yesterday, now=now) is None

    historical_end = now.date() - timedelta(days=10)
    cache.set_frame(
        "fixture",
        "NVDA",
        "1d",
        start,
        historical_end,
        frame,
        now=now - timedelta(days=365),
    )
    assert cache.get_frame("fixture", "NVDA", "1d", start, historical_end, now=now) is not None
    assert ttl_for("1d", historical_end, now=now) is None

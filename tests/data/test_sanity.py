from __future__ import annotations

import logging

import pandas as pd

from sentinel.data.sanity import clean_ohlcv


def test_clean_ohlcv_drops_bad_bars_and_logs_count(caplog) -> None:
    frame = pd.DataFrame(
        {
            "open": [10, 10, -1, 15, 8],
            "high": [12, 9, 12, 14, 12],
            "low": [9, 11, 9, 9, 9],
            "close": [11, 10, 10, 10, 13],
            "volume": [100, 100, 100, 100, 100],
        },
        index=pd.date_range("2025-01-01", periods=5),
    )

    with caplog.at_level(logging.WARNING):
        clean = clean_ohlcv(frame)

    assert len(clean) == 1
    assert clean.iloc[0]["close"] == 11
    assert "Dropped 4 invalid OHLCV bar(s)" in caplog.text

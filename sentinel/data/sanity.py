"""Sanity guards for OHLCV bars."""

from __future__ import annotations

import logging

import pandas as pd

from sentinel.data.loaders.base import OHLCV_COLUMNS, empty_ohlcv

logger = logging.getLogger(__name__)


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Drop impossible OHLCV bars and log how many rows were removed."""

    if df is None or df.empty:
        return empty_ohlcv()
    missing = [column for column in OHLCV_COLUMNS if column not in df.columns]
    if missing:
        msg = f"OHLCV frame missing required columns: {missing}"
        raise ValueError(msg)

    working = df.copy()
    for column in OHLCV_COLUMNS:
        working[column] = pd.to_numeric(working[column], errors="coerce")

    price_positive = (working[["open", "high", "low", "close"]] > 0).all(axis=1)
    high_low_ok = working["high"] >= working["low"]
    open_ok = (working["low"] <= working["open"]) & (working["open"] <= working["high"])
    close_ok = (working["low"] <= working["close"]) & (working["close"] <= working["high"])
    valid = price_positive & high_low_ok & open_ok & close_ok

    dropped = int((~valid).sum())
    if dropped:
        logger.warning("Dropped %s invalid OHLCV bar(s)", dropped)
    return working.loc[valid].copy()

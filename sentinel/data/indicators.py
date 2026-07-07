"""Deterministic technical indicators computed from OHLCV bars."""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *df* with Sentinel's Phase 1 technical indicators."""

    if df.empty:
        return df.copy()

    out = df.copy().sort_index()
    high = _numeric_series(out, "high")
    low = _numeric_series(out, "low")
    close = _numeric_series(out, "close")
    volume = _numeric_series(out, "volume").fillna(0)

    out["sma_20"] = close.rolling(window=20, min_periods=20).mean()
    out["sma_50"] = close.rolling(window=50, min_periods=50).mean()
    out["sma_200"] = close.rolling(window=200, min_periods=200).mean()
    out["ema_12"] = close.ewm(span=12, adjust=False, min_periods=12).mean()
    out["ema_26"] = close.ewm(span=26, adjust=False, min_periods=26).mean()
    out["macd"] = out["ema_12"] - out["ema_26"]
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi_14"] = 100 - (100 / (1 + rs))
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi_14"] = 100.0

    out["bb_mid_20"] = out["sma_20"]
    bb_std = close.rolling(window=20, min_periods=20).std(ddof=0)
    out["bb_upper_20"] = out["bb_mid_20"] + (2 * bb_std)
    out["bb_lower_20"] = out["bb_mid_20"] - (2 * bb_std)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = true_range.rolling(window=14, min_periods=14).mean()

    direction = pd.Series(np.sign(close.diff().to_numpy()), index=close.index).fillna(0)
    out["obv"] = (direction * volume).cumsum()

    returns = close.pct_change()
    out["realized_vol_30"] = returns.rolling(window=30, min_periods=30).std(ddof=0) * np.sqrt(252)
    out["return_1d"] = close.pct_change(periods=1)
    out["return_5d"] = close.pct_change(periods=5)
    out["return_20d"] = close.pct_change(periods=20)

    return out


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    return cast(pd.Series, pd.to_numeric(df[column], errors="coerce"))

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from sentinel.data.indicators import compute_indicators


def test_compute_indicators_matches_known_fixture_values() -> None:
    fixture = Path(__file__).parent / "fixtures" / "ohlcv_fixture.csv"
    frame = pd.read_csv(fixture, parse_dates=["date"]).set_index("date")

    result = compute_indicators(frame)
    last = result.iloc[-1]

    expected = {
        "sma_20": 152.125,
        "sma_50": 140.871,
        "ema_12": 155.05521713002508,
        "ema_26": 149.86333433564485,
        "macd": 5.191882794380234,
        "macd_signal": 5.215490380322598,
        "macd_hist": -0.023607585942364118,
        "rsi_14": 92.19274616405487,
        "bb_mid_20": 152.125,
        "bb_upper_20": 160.66692601232367,
        "bb_lower_20": 143.58307398767633,
        "atr_14": 2.680000000000003,
        "obv": 59759224.0,
        "realized_vol_30": 0.05130791935702093,
        "return_1d": 0.006008855154965298,
        "return_5d": 0.02151573538856799,
        "return_20d": 0.10259965337954946,
    }
    for column, value in expected.items():
        assert last[column] == pytest.approx(value)


def test_compute_indicators_is_pure() -> None:
    fixture = Path(__file__).parent / "fixtures" / "ohlcv_fixture.csv"
    frame = pd.read_csv(fixture, parse_dates=["date"]).set_index("date")
    original_columns = list(frame.columns)

    result = compute_indicators(frame)

    assert list(frame.columns) == original_columns
    assert "sma_20" in result.columns

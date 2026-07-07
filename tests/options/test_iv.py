import math

import pytest

from sentinel.options.iv import (
    implied_vol,
    iv_percentile,
    iv_rank,
    parkinson,
    realized_vol_close_to_close,
    yang_zhang,
)
from sentinel.options.pricing import bsm_price


def test_implied_vol_round_trip() -> None:
    price = bsm_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.37, "call")
    assert implied_vol(price, 100.0, 100.0, 1.0, 0.05, 0.0, "call") == pytest.approx(
        0.37, abs=1e-6
    )


def test_implied_vol_no_arbitrage_violations() -> None:
    assert implied_vol(-0.01, 100.0, 100.0, 1.0, 0.05, 0.0, "call") is None
    assert implied_vol(101.0, 100.0, 100.0, 1.0, 0.05, 0.0, "call") is None
    assert implied_vol(96.0, 100.0, 100.0, 1.0, 0.05, 0.0, "put") is None


def test_realized_vol_estimators_are_annualized() -> None:
    closes = [100.0, 101.0, 99.0, 102.0]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    expected_var = sum((ret - mean) ** 2 for ret in returns) / (len(returns) - 1)
    assert realized_vol_close_to_close(closes) == pytest.approx(math.sqrt(expected_var * 252.0))

    highs = [102.0, 103.0, 104.0, 105.0]
    lows = [99.0, 98.0, 100.0, 101.0]
    assert parkinson(highs, lows) > 0.0
    assert yang_zhang([100.0, 101.0, 100.0, 103.0], highs, lows, closes) > 0.0


def test_iv_rank_and_percentile() -> None:
    history = [0.10, 0.20, 0.30, 0.40]
    assert iv_rank(0.25, history) == pytest.approx(0.5)
    assert iv_percentile(0.25, history) == pytest.approx(0.5)
    assert iv_rank(0.25, [0.20, 0.20]) == 0.0
    assert iv_percentile(0.25, []) == 0.0

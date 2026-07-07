from __future__ import annotations

import pandas as pd
import pytest

from sentinel.backtest.metrics import compute_metrics
from sentinel.backtest.walkforward import walk_forward_metrics


def test_walk_forward_metrics_splits_returns_deterministically() -> None:
    returns = pd.Series(
        [0.01, 0.02, -0.03, 0.01, 0.01, -0.30],
        index=pd.bdate_range("2025-01-01", periods=6),
    )

    windows, consistency_rate = walk_forward_metrics(returns, n_windows=3)

    assert [window.periods for window in windows] == [2, 2, 2]
    assert [window.start for window in windows] == [
        pd.Timestamp("2025-01-01").date(),
        pd.Timestamp("2025-01-03").date(),
        pd.Timestamp("2025-01-07").date(),
    ]
    assert windows[0].total_return == pytest.approx((1.01 * 1.02) - 1.0)
    assert windows[2].max_drawdown == pytest.approx(-0.30)
    assert [window.consistent for window in windows] == [True, False, False]
    assert consistency_rate == pytest.approx(1 / 3)
    assert windows == walk_forward_metrics(returns, n_windows=3)[0]


def test_walk_forward_metrics_validates_window_count() -> None:
    with pytest.raises(ValueError, match="n_windows must be positive"):
        walk_forward_metrics([0.01], n_windows=0)


def test_compute_metrics_populates_walk_forward_fields() -> None:
    equity_curve = [
        {"date": "2025-01-01", "equity": 100.0},
        {"date": "2025-01-02", "equity": 101.0},
        {"date": "2025-01-03", "equity": 102.01},
        {"date": "2025-01-06", "equity": 103.0301},
        {"date": "2025-01-07", "equity": 104.060401},
        {"date": "2025-01-08", "equity": 105.10100501},
    ]

    result = compute_metrics(
        equity_curve=equity_curve,
        trades=[],
        starting_cash=100.0,
        bootstrap_iterations=0,
    )

    assert result.walk_forward is not None
    assert len(result.walk_forward) == 5
    assert result.walk_forward_consistency == pytest.approx(0.0)
    assert result.model_dump(mode="json")["walk_forward"][0]["periods"] == 1

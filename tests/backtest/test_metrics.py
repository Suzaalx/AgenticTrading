from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from sentinel.backtest.metrics import compute_metrics


def test_metrics_match_hand_computed_values() -> None:
    equity_curve = [
        {"date": "2025-01-01", "equity": 100.0},
        {"date": "2025-01-02", "equity": 110.0},
        {"date": "2025-01-03", "equity": 105.0},
        {"date": "2025-01-06", "equity": 120.0},
    ]
    trades = [{"pnl": 10.0}, {"pnl": -5.0}]
    benchmark = pd.DataFrame(
        {"close": [100.0, 110.0]},
        index=[pd.Timestamp("2025-01-01"), pd.Timestamp("2025-01-06")],
    )

    result = compute_metrics(
        equity_curve=equity_curve,
        trades=trades,
        starting_cash=100.0,
        benchmark_symbol="SPY",
        benchmark_prices=benchmark,
        exposure_steps=2,
        total_steps=4,
        turnover_notional=50.0,
        bootstrap_iterations=0,
    )

    returns = np.array([0.10, 105.0 / 110.0 - 1.0, 120.0 / 105.0 - 1.0])
    expected_sharpe = (returns.mean() / returns.std(ddof=1)) * np.sqrt(252)
    expected_annualized = ((1.0 + 0.20) ** (252.0 / len(returns))) - 1.0
    benchmark_returns = np.array([0.0, 0.0, 0.10])
    active_returns = returns - benchmark_returns
    expected_information_ratio = (
        active_returns.mean() / active_returns.std(ddof=1)
    ) * np.sqrt(252)

    assert result.total_return == pytest.approx(0.20)
    assert result.annualized_return == pytest.approx(expected_annualized)
    assert result.calmar == pytest.approx(expected_annualized / abs(105.0 / 110.0 - 1.0))
    assert result.sharpe == pytest.approx(expected_sharpe)
    assert result.information_ratio == pytest.approx(expected_information_ratio)
    assert result.max_drawdown == pytest.approx(105.0 / 110.0 - 1.0)
    assert result.max_drawdown_start == date(2025, 1, 2)
    assert result.max_drawdown_end == date(2025, 1, 3)
    assert result.win_rate == pytest.approx(0.5)
    assert result.profit_factor == pytest.approx(2.0)
    assert result.avg_win == pytest.approx(10.0)
    assert result.avg_loss == pytest.approx(-5.0)
    assert result.exposure_pct == pytest.approx(0.5)
    assert result.benchmark_return == pytest.approx(0.10)
    assert result.alpha == pytest.approx(0.10)

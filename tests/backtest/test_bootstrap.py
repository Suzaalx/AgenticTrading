from __future__ import annotations

from sentinel.backtest.bootstrap import bootstrap_return_metrics


def test_seeded_bootstrap_is_stable() -> None:
    returns = [0.01, -0.02, 0.015, 0.005, -0.01]

    first = bootstrap_return_metrics(returns, iterations=200, seed=42)
    second = bootstrap_return_metrics(returns, iterations=200, seed=42)

    assert first == second
    assert first["sharpe_p05"] is not None
    assert first["sharpe_p95"] is not None
    assert first["max_drawdown_p05"] is not None
    assert first["max_drawdown_p95"] is not None

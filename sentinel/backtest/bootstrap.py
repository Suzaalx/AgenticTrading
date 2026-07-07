"""Bootstrap robustness estimates for backtest daily returns."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


def bootstrap_return_metrics(
    daily_returns: Sequence[float] | pd.Series,
    *,
    iterations: int = 1000,
    seed: int = 0,
) -> dict[str, float | None]:
    """Resample daily returns and return 5th/95th percentile Sharpe and max drawdown."""

    returns = np.asarray(list(daily_returns), dtype=float)
    returns = returns[np.isfinite(returns)]
    if iterations <= 0 or returns.size == 0:
        return {
            "sharpe_p05": None,
            "sharpe_p95": None,
            "max_drawdown_p05": None,
            "max_drawdown_p95": None,
        }

    rng = np.random.default_rng(seed)
    sharpes = np.empty(iterations, dtype=float)
    drawdowns = np.empty(iterations, dtype=float)
    for index in range(iterations):
        sample = rng.choice(returns, size=returns.size, replace=True)
        sharpes[index] = _annualized_sharpe(sample)
        drawdowns[index] = _max_drawdown(sample)

    return {
        "sharpe_p05": float(np.percentile(sharpes, 5)),
        "sharpe_p95": float(np.percentile(sharpes, 95)),
        "max_drawdown_p05": float(np.percentile(drawdowns, 5)),
        "max_drawdown_p95": float(np.percentile(drawdowns, 95)),
    }


def _annualized_sharpe(returns: np.ndarray) -> float:
    if returns.size == 0:
        return 0.0
    std = returns.std(ddof=1) if returns.size > 1 else 0.0
    if std == 0:
        return 0.0
    return float((returns.mean() / std) * np.sqrt(252))


def _max_drawdown(returns: np.ndarray) -> float:
    if returns.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(equity)
    drawdowns = (equity / peaks) - 1.0
    return float(drawdowns.min())

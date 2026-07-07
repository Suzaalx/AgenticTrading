"""Walk-forward robustness metrics for backtest return series."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

import numpy as np
import pandas as pd

from sentinel.core.models import BacktestWalkForwardWindow


def walk_forward_metrics(
    returns: Sequence[float] | pd.Series,
    *,
    n_windows: int = 5,
    max_drawdown_floor: float = -0.20,
) -> tuple[list[BacktestWalkForwardWindow], float]:
    """Split ordered returns into windows and summarize robustness consistency."""

    if n_windows <= 0:
        msg = "n_windows must be positive"
        raise ValueError(msg)

    series = _clean_return_series(returns)
    if series.empty:
        return [], 0.0

    window_count = min(n_windows, len(series))
    windows: list[BacktestWalkForwardWindow] = []
    positions = np.arange(len(series))
    for index, split in enumerate(np.array_split(positions, window_count), start=1):
        if split.size == 0:
            continue
        window_returns = cast(pd.Series, series.iloc[split])
        total_return = _total_return(window_returns)
        sharpe = _sharpe(window_returns)
        max_drawdown = _max_drawdown(window_returns)
        consistent = sharpe > 0.0 and total_return > 0.0 and max_drawdown > max_drawdown_floor
        windows.append(
            BacktestWalkForwardWindow(
                window=index,
                start=_date_or_none(window_returns.index[0]),
                end=_date_or_none(window_returns.index[-1]),
                periods=len(window_returns),
                total_return=total_return,
                sharpe=sharpe,
                max_drawdown=max_drawdown,
                consistent=consistent,
            )
        )

    if not windows:
        return [], 0.0
    consistency_rate = sum(1 for window in windows if window.consistent) / len(windows)
    return windows, float(consistency_rate)


def _clean_return_series(returns: Sequence[float] | pd.Series) -> pd.Series:
    raw = returns if isinstance(returns, pd.Series) else pd.Series(list(returns), dtype=float)
    clean = pd.Series(
        pd.to_numeric(raw, errors="coerce"),
        index=raw.index,
        dtype=float,
    ).replace([np.inf, -np.inf], np.nan)
    return cast(pd.Series, clean.dropna())


def _total_return(returns: pd.Series) -> float:
    compounded = cast(pd.Series, (1.0 + returns).cumprod())
    return float(compounded.iloc[-1] - 1.0)


def _sharpe(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    std = float(cast(Any, returns.std(ddof=1)))
    if std == 0.0 or not math.isfinite(std):
        return 0.0
    return float((float(cast(Any, returns.mean())) / std) * np.sqrt(252))


def _max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = cast(pd.Series, (1.0 + returns).cumprod())
    peaks = cast(pd.Series, equity.cummax())
    drawdowns = cast(pd.Series, (equity / peaks) - 1.0)
    value = float(cast(Any, drawdowns.min()))
    return min(0.0, value)


def _date_or_none(value: object) -> date | None:
    if isinstance(value, int):
        return None
    try:
        return cast(date, pd.Timestamp(cast(Any, value)).date())
    except (TypeError, ValueError, OverflowError):
        return None

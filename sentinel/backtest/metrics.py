"""Backtest performance metric calculations."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast

import numpy as np
import pandas as pd

from sentinel.backtest.bootstrap import bootstrap_return_metrics
from sentinel.core.models import BacktestResult


def compute_metrics(
    *,
    equity_curve: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    starting_cash: float,
    benchmark_symbol: str = "SPY",
    benchmark_prices: pd.DataFrame | pd.Series | None = None,
    exposure_steps: int = 0,
    total_steps: int | None = None,
    turnover_notional: float = 0.0,
    bootstrap_iterations: int = 1000,
    bootstrap_seed: int = 0,
) -> BacktestResult:
    """Compute a fully populated ``BacktestResult`` from an equity curve and trades."""

    equity = _equity_series(equity_curve)
    if equity.empty:
        msg = "equity_curve must contain at least one point"
        raise ValueError(msg)
    final_equity = float(equity.iloc[-1])
    total_return = (final_equity / starting_cash) - 1.0 if starting_cash else 0.0
    returns = cast(pd.Series, equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna())
    periods = max(len(returns), 1)
    annualized_return = ((1.0 + total_return) ** (252.0 / periods)) - 1.0
    sharpe = _sharpe(returns)
    sortino = _sortino(returns)
    max_drawdown, drawdown_start, drawdown_end = _drawdown(equity)
    closed_pnls = [float(trade["pnl"]) for trade in trades if "pnl" in trade]
    wins = [pnl for pnl in closed_pnls if pnl > 0]
    losses = [pnl for pnl in closed_pnls if pnl < 0]
    win_rate = len(wins) / len(closed_pnls) if closed_pnls else 0.0
    profit_factor = _profit_factor(wins, losses)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    denominator_steps = total_steps if total_steps is not None else len(equity)
    exposure_pct = exposure_steps / denominator_steps if denominator_steps else 0.0
    average_equity = float(equity.mean()) if not equity.empty else starting_cash
    turnover = turnover_notional / average_equity if average_equity else 0.0
    benchmark_return = _benchmark_return(benchmark_prices, equity.index)
    bootstrap = bootstrap_return_metrics(
        returns,
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )

    return BacktestResult(
        total_return=total_return,
        annualized_return=annualized_return,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        max_drawdown_start=drawdown_start,
        max_drawdown_end=drawdown_end,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        exposure_pct=exposure_pct,
        turnover=turnover,
        trades=[dict(trade) for trade in trades],
        equity_curve=[dict(point) for point in equity_curve],
        benchmark_symbol=benchmark_symbol,
        benchmark_return=benchmark_return,
        alpha=total_return - benchmark_return,
        bootstrap_sharpe_p05=bootstrap["sharpe_p05"],
        bootstrap_sharpe_p95=bootstrap["sharpe_p95"],
        bootstrap_max_drawdown_p05=bootstrap["max_drawdown_p05"],
        bootstrap_max_drawdown_p95=bootstrap["max_drawdown_p95"],
    )


def _equity_series(equity_curve: Sequence[Mapping[str, Any]]) -> pd.Series:
    dates: list[pd.Timestamp] = []
    values: list[float] = []
    for point in equity_curve:
        ts = point.get("date", point.get("ts"))
        if ts is None:
            msg = "each equity point must include date or ts"
            raise ValueError(msg)
        stamp = cast(pd.Timestamp, pd.Timestamp(cast(Any, ts)))
        dates.append(cast(pd.Timestamp, pd.Timestamp(stamp.date())))
        values.append(float(point["equity"]))
    return pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float)


def _sharpe(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    std = float(cast(Any, returns.std(ddof=1)))
    if std == 0.0 or not math.isfinite(std):
        return 0.0
    return float((float(cast(Any, returns.mean())) / std) * np.sqrt(252))


def _sortino(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    downside = cast(pd.Series, returns[returns < 0])
    if downside.empty:
        return 0.0
    downside_std = float(cast(Any, downside.std(ddof=1))) if len(downside) > 1 else 0.0
    if downside_std == 0.0 or not math.isfinite(downside_std):
        return 0.0
    return float((float(cast(Any, returns.mean())) / downside_std) * np.sqrt(252))


def _drawdown(equity: pd.Series) -> tuple[float, date | None, date | None]:
    running_peak = cast(pd.Series, equity.cummax())
    drawdowns = cast(pd.Series, (equity / running_peak) - 1.0)
    min_drawdown = float(cast(Any, drawdowns.min()))
    if min_drawdown >= 0.0:
        return 0.0, None, None
    end_ts = cast(pd.Timestamp, pd.Timestamp(cast(Any, drawdowns.idxmin())))
    peak_ts = cast(pd.Timestamp, pd.Timestamp(cast(Any, equity.loc[:end_ts].idxmax())))
    return min_drawdown, cast(date, peak_ts.date()), cast(date, end_ts.date())


def _profit_factor(wins: Sequence[float], losses: Sequence[float]) -> float:
    gross_profit = float(sum(wins))
    gross_loss = abs(float(sum(losses)))
    if gross_loss == 0.0:
        return math.inf if gross_profit > 0.0 else 0.0
    return gross_profit / gross_loss


def _benchmark_return(benchmark_prices: pd.DataFrame | pd.Series | None, equity_index: pd.Index) -> float:
    if benchmark_prices is None or len(benchmark_prices) == 0 or equity_index.empty:
        return 0.0
    if isinstance(benchmark_prices, pd.DataFrame):
        if "close" not in benchmark_prices.columns:
            return 0.0
        raw_series = cast(pd.Series, benchmark_prices["close"])
    else:
        raw_series = benchmark_prices
    prices = pd.Series(
        pd.to_numeric(raw_series, errors="coerce"),
        index=raw_series.index,
        dtype=float,
    )
    prices.index = [
        cast(pd.Timestamp, pd.Timestamp(cast(Any, value))).normalize() for value in prices.index
    ]
    prices = cast(pd.Series, prices.sort_index().dropna())
    if prices.empty:
        return 0.0
    equity_dates = pd.DatetimeIndex([pd.Timestamp(cast(Any, value)) for value in equity_index])
    start = equity_dates.min()
    end = equity_dates.max()
    scoped = prices.loc[(prices.index >= start) & (prices.index <= end)]
    if len(scoped) < 2:
        return 0.0
    first = float(scoped.iloc[0])
    last = float(scoped.iloc[-1])
    return (last / first) - 1.0 if first else 0.0

"""Config-driven Sentinel backtesting engine."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import pandas as pd

from sentinel.backtest.metrics import compute_metrics
from sentinel.backtest.strategies import (
    BuyHoldStrategy,
    RSIMeanRevertStrategy,
    SMACrossStrategy,
    Strategy,
)
from sentinel.backtest.strategies.base import BarContext
from sentinel.config.settings import load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import BacktestProgress
from sentinel.core.models import BacktestResult, DataSnapshot, Signal
from sentinel.data.indicators import compute_indicators
from sentinel.store.db import connect, run_migrations, sentinel_home

BacktestMode = Literal["rule", "agent"]
AgentDecision = Signal | Mapping[str, Any] | Any


class AgentPipeline(Protocol):
    """Agent replay seam supplied by the future orchestrator."""

    def __call__(self, snapshot: DataSnapshot) -> Awaitable[AgentDecision]:
        """Return a decision from a point-in-time ``DataSnapshot``."""
        ...


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest engine configuration."""

    symbol: str
    mode: BacktestMode = "rule"
    start: date | None = None
    end: date | None = None
    strategy: str | Strategy = "sma_cross"
    strategy_params: Mapping[str, Any] | None = None
    cadence: str | int = "weekly"
    starting_cash: float | None = None
    commission_usd: float | None = None
    slippage_bps: float | None = None
    benchmark_symbol: str = "SPY"
    csv_path: Path | None = None
    data: pd.DataFrame | None = None
    benchmark_data: pd.DataFrame | None = None
    persist: bool = True
    db_path: Path | None = None
    bt_id: str | None = None
    bootstrap_iterations: int = 1000
    bootstrap_seed: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | BacktestConfig) -> BacktestConfig:
        if isinstance(raw, BacktestConfig):
            return raw
        values = dict(raw)
        if values.get("csv_path") is not None:
            values["csv_path"] = Path(values["csv_path"])
        if values.get("db_path") is not None:
            values["db_path"] = Path(values["db_path"])
        for key in ("start", "end"):
            if isinstance(values.get(key), str):
                values[key] = date.fromisoformat(cast(str, values[key]))
        return cls(**values)

    def persisted_dict(self) -> dict[str, Any]:
        strategy_name = self.strategy if isinstance(self.strategy, str) else type(self.strategy).__name__
        return {
            "symbol": self.symbol,
            "mode": self.mode,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "strategy": strategy_name,
            "strategy_params": dict(self.strategy_params or {}),
            "cadence": self.cadence,
            "starting_cash": self.starting_cash,
            "commission_usd": self.commission_usd,
            "slippage_bps": self.slippage_bps,
            "benchmark_symbol": self.benchmark_symbol,
            "csv_path": str(self.csv_path) if self.csv_path else None,
            "bootstrap_iterations": self.bootstrap_iterations,
            "bootstrap_seed": self.bootstrap_seed,
        }


def run_backtest(
    config: BacktestConfig | Mapping[str, Any],
    *,
    pipeline: AgentPipeline | Callable[[DataSnapshot], Awaitable[AgentDecision]] | None = None,
    event_bus: EventBus | None = None,
) -> BacktestResult:
    """Run a backtest synchronously and return ``BacktestResult``."""

    return asyncio.run(run_backtest_async(config, pipeline=pipeline, event_bus=event_bus))


async def run_backtest_async(
    config: BacktestConfig | Mapping[str, Any],
    *,
    pipeline: AgentPipeline | Callable[[DataSnapshot], Awaitable[AgentDecision]] | None = None,
    event_bus: EventBus | None = None,
) -> BacktestResult:
    """Run a rule or agent replay backtest."""

    cfg = BacktestConfig.from_mapping(config)
    settings = load_settings(Path.cwd())
    starting_cash = cfg.starting_cash or settings.execution.starting_cash_usd
    commission_usd = cfg.commission_usd
    if commission_usd is None:
        commission_usd = settings.execution.commission_usd
    slippage_bps = cfg.slippage_bps
    if slippage_bps is None:
        slippage_bps = float(settings.execution.slippage_bps)
    bt_id = cfg.bt_id or f"bt_{uuid4().hex}"
    raw_frame = _load_ohlcv(cfg)
    frame = compute_indicators(raw_frame)
    strategy = _strategy_from_config(cfg)
    result = await _simulate(
        cfg=cfg,
        bt_id=bt_id,
        frame=frame,
        raw_frame=raw_frame,
        strategy=strategy,
        pipeline=pipeline,
        event_bus=event_bus,
        starting_cash=float(starting_cash),
        commission_usd=float(commission_usd),
        slippage_bps=float(slippage_bps),
    )
    if cfg.persist:
        _persist_backtest(bt_id, cfg, result)
    return result


async def _simulate(
    *,
    cfg: BacktestConfig,
    bt_id: str,
    frame: pd.DataFrame,
    raw_frame: pd.DataFrame,
    strategy: Strategy,
    pipeline: AgentPipeline | Callable[[DataSnapshot], Awaitable[AgentDecision]] | None,
    event_bus: EventBus | None,
    starting_cash: float,
    commission_usd: float,
    slippage_bps: float,
) -> BacktestResult:
    if frame.empty:
        msg = "backtest requires at least one OHLCV bar"
        raise ValueError(msg)
    if cfg.mode == "agent" and pipeline is None:
        msg = "agent mode requires an injected async pipeline callable"
        raise ValueError(msg)

    cash = starting_cash
    shares = 0.0
    avg_cost = 0.0
    entry_date: date | None = None
    entry_index: int | None = None
    pending_signal: Signal | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    exposure_steps = 0
    turnover_notional = 0.0

    total_steps = len(frame)
    for index, (timestamp, raw_row) in enumerate(frame.iterrows()):
        row = cast(pd.Series, raw_row)
        ts = cast(pd.Timestamp, pd.Timestamp(cast(Any, timestamp)))
        open_price = float(cast(Any, row["open"]))
        close_price = float(cast(Any, row["close"]))
        if pending_signal is not None:
            executed = _execute_signal(
                signal=pending_signal,
                symbol=cfg.symbol,
                ts=ts,
                bar_index=index,
                open_price=open_price,
                close_price=close_price,
                cash=cash,
                shares=shares,
                avg_cost=avg_cost,
                entry_date=entry_date,
                entry_index=entry_index,
                commission_usd=commission_usd,
                slippage_bps=slippage_bps,
            )
            cash = executed.cash
            shares = executed.shares
            avg_cost = executed.avg_cost
            entry_date = executed.entry_date
            entry_index = executed.entry_index
            turnover_notional += executed.turnover_notional
            trades.extend(executed.trades)
            pending_signal = None

        equity = cash + (shares * close_price)
        if shares > 0:
            exposure_steps += 1
        equity_curve.append(
            {
                "date": ts.date().isoformat(),
                "equity": equity,
                "cash": cash,
                "position_qty": shares,
                "close": close_price,
            }
        )

        if cfg.mode == "rule":
            history = frame.iloc[: index + 1].copy()
            context = BarContext(
                symbol=cfg.symbol,
                timestamp=ts,
                bar=row,
                history=history,
                cash=cash,
                position_qty=shares,
                position_value=shares * close_price,
                equity=equity,
            )
            pending_signal = strategy.on_bar(context)
        elif _is_cadence_step(index, cfg.cadence):
            snapshot = _build_agent_snapshot(
                bt_id=bt_id,
                symbol=cfg.symbol,
                ts=ts,
                raw_history=raw_frame.iloc[: index + 1],
            )
            decision = await cast(Callable[[DataSnapshot], Awaitable[AgentDecision]], pipeline)(snapshot)
            pending_signal = _decision_to_signal(decision)

        await _publish_progress(event_bus, bt_id, index + 1, total_steps)

    benchmark_data = cfg.benchmark_data
    if benchmark_data is None and cfg.benchmark_symbol.upper() == cfg.symbol.upper():
        benchmark_data = raw_frame
    return compute_metrics(
        equity_curve=equity_curve,
        trades=trades,
        starting_cash=starting_cash,
        benchmark_symbol=cfg.benchmark_symbol,
        benchmark_prices=benchmark_data,
        exposure_steps=exposure_steps,
        total_steps=total_steps,
        turnover_notional=turnover_notional,
        bootstrap_iterations=cfg.bootstrap_iterations,
        bootstrap_seed=cfg.bootstrap_seed,
    )


@dataclass(frozen=True)
class _ExecutionResult:
    cash: float
    shares: float
    avg_cost: float
    entry_date: date | None
    entry_index: int | None
    turnover_notional: float
    trades: list[dict[str, Any]]


def _execute_signal(
    *,
    signal: Signal,
    symbol: str,
    ts: pd.Timestamp,
    bar_index: int,
    open_price: float,
    close_price: float,
    cash: float,
    shares: float,
    avg_cost: float,
    entry_date: date | None,
    entry_index: int | None,
    commission_usd: float,
    slippage_bps: float,
) -> _ExecutionResult:
    action = signal.action.upper()
    size = _bounded_size(signal.size)
    trades: list[dict[str, Any]] = []
    turnover_notional = 0.0
    if action == "BUY":
        fill_price = open_price * (1.0 + (slippage_bps / 10_000.0))
        equity_at_open = cash + (shares * open_price)
        target_value = equity_at_open * size
        current_value = shares * open_price
        buy_notional = max(0.0, target_value - current_value)
        available_notional = max(0.0, cash - commission_usd)
        buy_notional = min(buy_notional, available_notional)
        qty = buy_notional / fill_price if fill_price > 0 else 0.0
        if qty > 1e-12:
            previous_cost = shares * avg_cost
            cash -= (qty * fill_price) + commission_usd
            shares += qty
            avg_cost = (previous_cost + (qty * fill_price)) / shares
            if entry_date is None:
                entry_date = ts.date()
                entry_index = bar_index
            turnover_notional = qty * fill_price
    elif action == "SELL" and shares > 0:
        fill_price = open_price * (1.0 - (slippage_bps / 10_000.0))
        qty = min(shares, shares * size)
        if qty > 1e-12:
            notional = qty * fill_price
            cash += notional - commission_usd
            pnl = ((fill_price - avg_cost) * qty) - commission_usd
            turnover_notional = notional
            trades.append(
                {
                    "symbol": symbol,
                    "entry_date": entry_date.isoformat() if entry_date else None,
                    "entry_price": avg_cost,
                    "exit_date": ts.date().isoformat(),
                    "exit_price": fill_price,
                    "qty": qty,
                    "pnl": pnl,
                    "return": (fill_price / avg_cost) - 1.0 if avg_cost else 0.0,
                    "bars_held": (bar_index - entry_index) if entry_index is not None else None,
                }
            )
            shares -= qty
            if shares <= 1e-12:
                shares = 0.0
                avg_cost = 0.0
                entry_date = None
                entry_index = None
    return _ExecutionResult(
        cash=cash,
        shares=shares,
        avg_cost=avg_cost,
        entry_date=entry_date,
        entry_index=entry_index,
        turnover_notional=turnover_notional,
        trades=trades,
    )


def _load_ohlcv(cfg: BacktestConfig) -> pd.DataFrame:
    if cfg.data is not None:
        return _normalize_frame(cfg.data)
    if cfg.csv_path is not None:
        return _normalize_frame(_read_csv(cfg.csv_path))
    if cfg.start is None or cfg.end is None:
        msg = "start and end are required when no data frame or csv_path is supplied"
        raise ValueError(msg)
    from sentinel.data.router import DataRouter

    router = DataRouter(settings=load_settings(Path.cwd()))
    return _normalize_frame(router.get_ohlcv(cfg.symbol, cfg.start, cfg.end))


def _read_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    date_column = "date" if "date" in {str(col).lower() for col in frame.columns} else frame.columns[0]
    if date_column != frame.columns[0]:
        for column in frame.columns:
            if str(column).lower() == "date":
                date_column = column
                break
    frame[date_column] = pd.to_datetime(frame[date_column])
    return frame.set_index(date_column)


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(column).lower() for column in out.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(out.columns)
    if missing:
        msg = f"OHLCV frame missing columns: {sorted(missing)}"
        raise ValueError(msg)
    out.index = [
        cast(pd.Timestamp, pd.Timestamp(cast(Any, value))).normalize() for value in out.index
    ]
    out = out.sort_index()
    return cast(pd.DataFrame, out.loc[~out.index.duplicated(keep="last")])


def _strategy_from_config(cfg: BacktestConfig) -> Strategy:
    if not isinstance(cfg.strategy, str):
        return cfg.strategy
    params = dict(cfg.strategy_params or {})
    name = cfg.strategy.lower()
    if name == "sma_cross":
        return SMACrossStrategy(**params)
    if name == "rsi_meanrevert":
        return RSIMeanRevertStrategy(**params)
    if name == "buy_hold":
        return BuyHoldStrategy(**params)
    msg = f"unknown strategy: {cfg.strategy}"
    raise ValueError(msg)


def _bounded_size(size: float | None) -> float:
    if size is None:
        return 1.0
    return min(1.0, max(0.0, float(size)))


def _is_cadence_step(index: int, cadence: str | int) -> bool:
    if isinstance(cadence, int):
        return cadence > 0 and index % cadence == 0
    normalized = cadence.lower()
    if normalized == "daily":
        return True
    if normalized == "weekly":
        return index % 5 == 0
    if normalized == "monthly":
        return index % 21 == 0
    msg = "cadence must be daily, weekly, monthly, or a positive integer"
    raise ValueError(msg)


def _build_agent_snapshot(
    *,
    bt_id: str,
    symbol: str,
    ts: pd.Timestamp,
    raw_history: pd.DataFrame,
) -> DataSnapshot:
    snapshot_root = sentinel_home() / "backtests" / bt_id / ts.strftime("%Y%m%d")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    ohlcv_path = snapshot_root / "ohlcv.parquet"
    indicators_path = snapshot_root / "indicators.parquet"
    history = _normalize_frame(raw_history)
    indicators = compute_indicators(history)
    history.to_parquet(ohlcv_path)
    indicators.to_parquet(indicators_path)
    return DataSnapshot(
        run_id=f"{bt_id}:{ts.date().isoformat()}",
        symbol=symbol,
        as_of=ts.to_pydatetime().replace(tzinfo=UTC),
        ohlcv_path=str(ohlcv_path),
        indicators_path=str(indicators_path),
        news=[],
        fundamentals=None,
        providers_used={"ohlcv": "backtest_frame", "indicators": "computed"},
    )


def _decision_to_signal(decision: AgentDecision) -> Signal:
    if isinstance(decision, Signal):
        return decision
    if isinstance(decision, Mapping):
        action = str(decision.get("action", "HOLD")).upper()
        size = decision.get("size")
        return Signal(action=cast(Literal["BUY", "SELL", "HOLD"], action), size=size)
    action = str(getattr(decision, "action", "HOLD")).upper()
    size = getattr(decision, "size", None)
    return Signal(action=cast(Literal["BUY", "SELL", "HOLD"], action), size=size)


async def _publish_progress(
    event_bus: EventBus | None, bt_id: str, completed_steps: int, total_steps: int
) -> None:
    if event_bus is None:
        return
    await event_bus.publish(
        BacktestProgress(
            bt_id=bt_id,
            completed_steps=completed_steps,
            total_steps=total_steps,
            message=f"{completed_steps}/{total_steps} bars replayed",
        )
    )


def _persist_backtest(bt_id: str, cfg: BacktestConfig, result: BacktestResult) -> None:
    conn = connect(cfg.db_path)
    try:
        run_migrations(conn)
        metrics_json = json.dumps(result.model_dump(mode="json"), allow_nan=True)
        equity_json = json.dumps(result.equity_curve, allow_nan=True, default=str)
        conn.execute(
            """INSERT OR REPLACE INTO backtests
            (bt_id, config_json, metrics_json, equity_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                bt_id,
                json.dumps(cfg.persisted_dict(), default=str),
                metrics_json,
                equity_json,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

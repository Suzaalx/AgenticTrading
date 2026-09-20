"""Experiment runner: same bars, same fill model, several pipelines, one results table.

An :class:`ExperimentSpec` fixes the symbol list, the date window and the pipelines to
compare. Bars for each symbol are loaded **once** and handed to every pipeline, so the
rule baselines and the Sentinel agent variants literally see the same DataFrame. Fills
are simulated by :mod:`sentinel.backtest.engine` for all of them (decision on bar *t*,
fill at the open of *t+1*, configured slippage/commission).

Everything a run produces is stored under one experiment SQLite database
(``<out_dir>/experiment.db``): the engine's ``backtests`` row, the orchestrator's
``costs``/``reports`` rows for agent pipelines, and an ``eval_results`` row per
(pipeline, symbol). :func:`load_result_rows` rebuilds the results table from those rows.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.backtest.engine import BacktestConfig, run_backtest_async
from sentinel.config.settings import Settings, load_mandate, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.models import Mandate
from sentinel.eval.metrics import (
    ResultRow,
    build_result_row,
    decision_costs,
    render_results_table,
    results_to_csv,
    row_from_record,
)
from sentinel.eval.pipelines import BuiltPipeline, PipelineSpec, get_pipeline
from sentinel.llm.contracts import StructuredLLM
from sentinel.store.db import connect, run_migrations, sentinel_home


@dataclass(frozen=True)
class ExperimentSpec:
    """A reproducible experiment definition."""

    experiment_id: str
    symbols: Sequence[str]
    start: date
    end: date
    pipelines: Sequence[str]
    cadence: str | int = "weekly"  # agent pipelines decide on this cadence; rules decide every bar
    starting_cash: float | None = None
    commission_usd: float | None = None
    slippage_bps: float | None = None
    strategy_params: Mapping[str, Any] = field(default_factory=dict)
    csv_paths: Mapping[str, Path] = field(default_factory=dict)  # symbol -> offline OHLCV CSV
    benchmark_symbol: str | None = None  # None -> the traded symbol itself
    out_dir: Path | None = None
    bootstrap_iterations: int = 0  # bootstrap CIs are slow and not part of the comparison table

    def resolved_out_dir(self) -> Path:
        return self.out_dir or (sentinel_home() / "experiments" / self.experiment_id)

    def db_path(self) -> Path:
        return self.resolved_out_dir() / "experiment.db"

    def csv_path(self) -> Path:
        return self.resolved_out_dir() / "results.csv"

    def to_json(self) -> str:
        return json.dumps(
            {
                "experiment_id": self.experiment_id,
                "symbols": list(self.symbols),
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "pipelines": list(self.pipelines),
                "cadence": self.cadence,
                "starting_cash": self.starting_cash,
                "commission_usd": self.commission_usd,
                "slippage_bps": self.slippage_bps,
                "strategy_params": dict(self.strategy_params),
                "csv_paths": {k: str(v) for k, v in self.csv_paths.items()},
                "benchmark_symbol": self.benchmark_symbol,
            },
            sort_keys=True,
            default=str,
        )


@dataclass
class ExperimentContext:
    """Shared dependencies handed to every pipeline builder."""

    spec: ExperimentSpec
    settings: Settings
    mandate: Mandate
    llm: StructuredLLM
    bus: EventBus
    conn: sqlite3.Connection
    router: Any | None
    starting_cash: float
    strategy_params: Mapping[str, Any]


def bt_id_for(spec: ExperimentSpec, pipeline: str, symbol: str) -> str:
    return f"{spec.experiment_id}__{pipeline}__{symbol.upper()}"


def run_experiment(spec: ExperimentSpec, **kwargs: Any) -> list[ResultRow]:
    return asyncio.run(run_experiment_async(spec, **kwargs))


async def run_experiment_async(
    spec: ExperimentSpec,
    *,
    settings: Settings | None = None,
    mandate: Mandate | None = None,
    llm: StructuredLLM | None = None,
    router: Any | None = None,
    bars: Mapping[str, pd.DataFrame] | None = None,
    conn: sqlite3.Connection | None = None,
    write_csv: bool = True,
    print_table: bool = False,
) -> list[ResultRow]:
    """Run every (pipeline, symbol) pair on identical bars and return one row per pair."""

    if not spec.pipelines:
        msg = "experiment needs at least one pipeline"
        raise ValueError(msg)
    specs = [get_pipeline(name) for name in spec.pipelines]
    settings = settings or load_settings(Path.cwd())
    mandate = mandate or load_mandate(Path.cwd())
    out_dir = spec.resolved_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    owned_conn = conn is None
    conn = conn or connect(spec.db_path())
    run_migrations(conn)
    if llm is None:
        from sentinel.llm.gateway import LLMGateway

        llm = LLMGateway(settings=settings)
    starting_cash = float(spec.starting_cash or settings.execution.starting_cash_usd)
    ctx = ExperimentContext(
        spec=spec,
        settings=settings,
        mandate=mandate,
        llm=llm,
        bus=EventBus(),
        conn=conn,
        router=router,
        starting_cash=starting_cash,
        strategy_params=spec.strategy_params,
    )
    frames = {symbol.upper(): frame for symbol, frame in (bars or {}).items()}
    rows: list[ResultRow] = []
    try:
        for symbol in spec.symbols:
            symbol = symbol.upper()
            frame = frames.get(symbol)
            if frame is None:
                frame = load_bars(spec, symbol, settings=settings, router=router)
                frames[symbol] = frame
            for pipeline_spec in specs:
                rows.append(await _run_one(ctx, pipeline_spec, symbol, frame))
        if write_csv:
            results_to_csv(rows, spec.csv_path())
        if print_table:
            render_results_table(rows)
    finally:
        if owned_conn:
            conn.close()
    return rows


async def _run_one(
    ctx: ExperimentContext, pipeline_spec: PipelineSpec, symbol: str, frame: pd.DataFrame
) -> ResultRow:
    spec = ctx.spec
    bt_id = bt_id_for(spec, pipeline_spec.name, symbol)
    _clear_previous_run(ctx.conn, bt_id)
    built: BuiltPipeline = pipeline_spec.build(ctx, symbol)
    benchmark = spec.benchmark_symbol or symbol
    config = BacktestConfig(
        symbol=symbol,
        mode="agent" if built.kind == "agent" else "rule",
        start=spec.start,
        end=spec.end,
        strategy=built.strategy if built.strategy is not None else "buy_hold",
        cadence=spec.cadence,
        starting_cash=ctx.starting_cash,
        commission_usd=spec.commission_usd,
        slippage_bps=spec.slippage_bps,
        benchmark_symbol=benchmark,
        data=frame,
        benchmark_data=frame if benchmark.upper() == symbol else None,
        persist=True,
        db_path=spec.db_path(),
        bt_id=bt_id,
        bootstrap_iterations=spec.bootstrap_iterations,
        options_enabled=False,
    )
    result = await run_backtest_async(config, pipeline=built.agent, event_bus=ctx.bus)
    decision_summary = built.summarize()
    row_config: dict[str, Any] = {
        **pipeline_spec.config,
        "cadence": spec.cadence if built.kind == "agent" else "daily",
    }
    if built.kind == "rule" and spec.strategy_params:
        row_config["strategy_params"] = {k: str(v) if isinstance(v, Path) else v for k, v in spec.strategy_params.items()}
    for key in ("policy", "stub"):
        if key in decision_summary:
            row_config[key] = decision_summary[key]
    row = build_result_row(
        experiment=spec.experiment_id,
        pipeline=pipeline_spec.name,
        config=row_config,
        symbol=symbol,
        start=spec.start.isoformat(),
        end=spec.end.isoformat(),
        bt_id=bt_id,
        result=result,
        bars=len(frame),
        decision_summary=decision_summary,
        costs=decision_costs(ctx.conn, bt_id),
        notes=built.notes(),
    )
    _persist_row(ctx.conn, spec, row)
    return row


def load_bars(
    spec: ExperimentSpec, symbol: str, *, settings: Settings | None = None, router: Any | None = None
) -> pd.DataFrame:
    """Load the OHLCV window once per symbol (CSV if given, else the data router)."""

    csv_path = spec.csv_paths.get(symbol) or spec.csv_paths.get(symbol.upper())
    if csv_path is not None:
        frame = pd.read_csv(csv_path)
        date_col = next((c for c in frame.columns if str(c).lower() == "date"), frame.columns[0])
        frame[date_col] = pd.to_datetime(frame[date_col])
        frame = frame.set_index(date_col)
    else:
        if router is None:
            from sentinel.data.router import DataRouter

            router = DataRouter(settings=settings or load_settings(Path.cwd()))
        frame = router.get_ohlcv(symbol, spec.start, spec.end)
    frame = frame.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    window = frame.loc[(frame.index >= pd.Timestamp(spec.start)) & (frame.index <= pd.Timestamp(spec.end))]
    if window.empty:
        msg = f"no bars for {symbol} in {spec.start}..{spec.end}"
        raise ValueError(msg)
    return window


def _clear_previous_run(conn: sqlite3.Connection, bt_id: str) -> None:
    """Re-running an experiment replaces its stored rows instead of double-counting costs."""

    like = f"{bt_id}:%"
    for table in ("costs", "reports", "runs"):
        conn.execute(f"DELETE FROM {table} WHERE run_id = ? OR run_id LIKE ?", (bt_id, like))
    conn.execute("DELETE FROM eval_results WHERE bt_id = ?", (bt_id,))
    conn.commit()


def _persist_row(conn: sqlite3.Connection, spec: ExperimentSpec, row: ResultRow) -> None:
    record = row.as_record()
    conn.execute(
        """INSERT OR REPLACE INTO eval_results
           (bt_id, experiment_id, pipeline, symbol, config_json, spec_json, metrics_json, equity_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row.bt_id,
            spec.experiment_id,
            row.pipeline,
            row.symbol,
            row.config,
            spec.to_json(),
            json.dumps(record, default=str),
            json.dumps(row.equity_curve, default=str),
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def load_result_rows(conn: sqlite3.Connection, experiment_id: str) -> list[ResultRow]:
    """Rebuild the results table for an experiment from stored ``eval_results`` rows."""

    rows = conn.execute(
        "SELECT bt_id, metrics_json, equity_json FROM eval_results WHERE experiment_id = ? ORDER BY symbol, pipeline",
        (experiment_id,),
    ).fetchall()
    out: list[ResultRow] = []
    for stored in rows:
        record = json.loads(stored["metrics_json"])
        record["bt_id"] = stored["bt_id"]
        record["equity_curve"] = json.loads(stored["equity_json"] or "[]")
        out.append(row_from_record(record))
    return out

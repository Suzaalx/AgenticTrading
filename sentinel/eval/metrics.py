"""Metrics for the evaluation harness: one tidy row per (pipeline, config, symbol, window).

Return-based metrics come from the backtest engine's ``BacktestResult`` (which already
computes total return, Sharpe, max drawdown and win rate from the equity curve / closed
trades). Cost and latency metrics come from the SQLite journal — the ``costs`` table rows
the agents write per LLM call, keyed by ``run_id = "<bt_id>:<date>"`` — so they can be
recomputed from stored runs without re-running anything.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from rich.console import Console

TRADING_DAYS_PER_YEAR = 252

# Column order for CSV and the printed table. Keep stable: downstream notebooks key on it.
RESULT_COLUMNS: tuple[str, ...] = (
    "experiment",
    "pipeline",
    "config",
    "symbol",
    "start",
    "end",
    "bars",
    "decisions",
    "signals",
    "round_trips",
    "total_return",
    "annualized_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "trade_frequency",
    "exposure_pct",
    "benchmark_return",
    "total_tokens",
    "total_cost_usd",
    "avg_tokens_per_decision",
    "avg_cost_usd_per_decision",
    "avg_llm_latency_ms_per_decision",
    "avg_wall_ms_per_decision",
    "failed_decisions",
    "notes",
)


@dataclass
class ResultRow:
    experiment: str
    pipeline: str
    config: str
    symbol: str
    start: str
    end: str
    bars: int
    decisions: int
    signals: int
    round_trips: int
    total_return: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trade_frequency: float
    exposure_pct: float
    benchmark_return: float
    total_tokens: int
    total_cost_usd: float
    avg_tokens_per_decision: float
    avg_cost_usd_per_decision: float
    avg_llm_latency_ms_per_decision: float
    avg_wall_ms_per_decision: float
    failed_decisions: int
    notes: str = ""
    bt_id: str = ""
    equity_curve: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def as_record(self) -> dict[str, Any]:
        """Row as a plain dict in ``RESULT_COLUMNS`` order (no equity curve)."""

        data = asdict(self)
        return {column: data[column] for column in RESULT_COLUMNS}


@dataclass(frozen=True)
class DecisionCostSummary:
    """Per-decision LLM metering aggregated from the ``costs`` table."""

    decisions_with_costs: int
    total_tokens: int
    total_cost_usd: float
    total_llm_latency_ms: int

    def per_decision(self, decisions: int) -> tuple[float, float, float]:
        if decisions <= 0:
            return (0.0, 0.0, 0.0)
        return (
            self.total_tokens / decisions,
            self.total_cost_usd / decisions,
            self.total_llm_latency_ms / decisions,
        )


def decision_costs(conn: sqlite3.Connection, bt_id: str) -> DecisionCostSummary:
    """Aggregate every LLM call whose ``run_id`` belongs to ``bt_id`` (``"<bt_id>:<date>"``)."""

    row = conn.execute(
        """SELECT COUNT(DISTINCT run_id) AS decisions,
                  COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens,
                  COALESCE(SUM(cost_usd), 0) AS cost,
                  COALESCE(SUM(latency_ms), 0) AS latency
           FROM costs WHERE run_id = ? OR run_id LIKE ?""",
        (bt_id, f"{bt_id}:%"),
    ).fetchone()
    return DecisionCostSummary(
        decisions_with_costs=int(row["decisions"] or 0),
        total_tokens=int(row["tokens"] or 0),
        total_cost_usd=float(row["cost"] or 0.0),
        total_llm_latency_ms=int(row["latency"] or 0),
    )


def trade_frequency(signals: int, bars: int) -> float:
    """Non-HOLD decisions per trading year, so daily and weekly cadences are comparable."""

    if bars <= 0:
        return 0.0
    return signals / bars * TRADING_DAYS_PER_YEAR


def build_result_row(
    *,
    experiment: str,
    pipeline: str,
    config: dict[str, Any],
    symbol: str,
    start: str,
    end: str,
    bt_id: str,
    result: Any,
    bars: int,
    decision_summary: dict[str, Any],
    costs: DecisionCostSummary,
    notes: list[str] | None = None,
) -> ResultRow:
    """Combine engine metrics, harness decision counters and SQLite cost metering."""

    decisions = int(decision_summary.get("decisions", 0))
    signals = int(decision_summary.get("signals", 0))
    avg_tokens, avg_cost, avg_llm_latency = costs.per_decision(decisions)
    return ResultRow(
        experiment=experiment,
        pipeline=pipeline,
        config=json.dumps(config, sort_keys=True, separators=(",", ":")),
        symbol=symbol,
        start=start,
        end=end,
        bars=bars,
        decisions=decisions,
        signals=signals,
        round_trips=len(result.trades),
        total_return=_finite(result.total_return),
        annualized_return=_finite(result.annualized_return),
        sharpe=_finite(result.sharpe),
        max_drawdown=_finite(result.max_drawdown),
        win_rate=_finite(result.win_rate),
        trade_frequency=trade_frequency(signals, bars),
        exposure_pct=_finite(result.exposure_pct),
        benchmark_return=_finite(result.benchmark_return),
        total_tokens=costs.total_tokens,
        total_cost_usd=costs.total_cost_usd,
        avg_tokens_per_decision=avg_tokens,
        avg_cost_usd_per_decision=avg_cost,
        avg_llm_latency_ms_per_decision=avg_llm_latency,
        avg_wall_ms_per_decision=float(decision_summary.get("avg_wall_ms", 0.0)),
        failed_decisions=int(decision_summary.get("failed_decisions", 0)),
        notes="; ".join(notes or []),
        bt_id=bt_id,
        equity_curve=list(result.equity_curve),
    )


def results_to_csv(rows: list[ResultRow], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RESULT_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_record())
    return path


SUMMARY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pipeline", "pipeline"),
    ("symbol", "symbol"),
    ("window", "window"),
    ("total_return", "ret"),
    ("sharpe", "sharpe"),
    ("max_drawdown", "maxDD"),
    ("win_rate", "win%"),
    ("trade_frequency", "trades/yr"),
    ("decisions", "decisions"),
    ("avg_tokens_per_decision", "tok/dec"),
    ("avg_cost_usd_per_decision", "$/dec"),
    ("avg_llm_latency_ms_per_decision", "llm_ms/dec"),
    ("avg_wall_ms_per_decision", "wall_ms/dec"),
    ("failed_decisions", "failed"),
    ("notes", "notes"),
)


def format_results_table(rows: list[ResultRow]) -> str:
    """Fixed-width text summary (never truncates columns, unlike a fitted rich table)."""

    cells: list[list[str]] = []
    for row in rows:
        cells.append(
            [
                row.pipeline,
                row.symbol,
                f"{row.start}..{row.end}",
                _pct(row.total_return),
                f"{row.sharpe:.2f}",
                _pct(row.max_drawdown),
                _pct(row.win_rate) if row.round_trips else "n/a",
                f"{row.trade_frequency:.1f}",
                str(row.decisions),
                f"{row.avg_tokens_per_decision:.0f}",
                f"{row.avg_cost_usd_per_decision:.4f}",
                f"{row.avg_llm_latency_ms_per_decision:.0f}",
                f"{row.avg_wall_ms_per_decision:.0f}",
                str(row.failed_decisions),
                row.notes[:80],
            ]
        )
    headers = [label for _, label in SUMMARY_COLUMNS]
    widths = [max(len(headers[i]), *(len(line[i]) for line in cells)) if cells else len(headers[i]) for i in range(len(headers))]
    left = {0, 1, 2, 14}

    def fmt(values: list[str]) -> str:
        return "  ".join(
            value.ljust(widths[i]) if i in left else value.rjust(widths[i]) for i, value in enumerate(values)
        ).rstrip()

    title = f"Experiment {rows[0].experiment}" if rows else "Experiment (no rows)"
    lines = [title, fmt(headers), fmt(["-" * w for w in widths]), *(fmt(line) for line in cells)]
    return "\n".join(lines)


def render_results_table(rows: list[ResultRow], *, console: Console | None = None) -> None:
    """Print the fixed-width summary table."""

    console = console or Console()
    console.print(format_results_table(rows), highlight=False, soft_wrap=True, markup=False)


def row_from_record(record: dict[str, Any]) -> ResultRow:
    """Rebuild a ``ResultRow`` from a stored/CSV record (missing columns default)."""

    allowed = {f.name for f in fields(ResultRow)}
    coerced: dict[str, Any] = {}
    for column in RESULT_COLUMNS:
        if column not in record:
            continue
        value = record[column]
        if column in {"bars", "decisions", "signals", "round_trips", "total_tokens", "failed_decisions"}:
            coerced[column] = int(value or 0)
        elif column in {"experiment", "pipeline", "config", "symbol", "start", "end", "notes"}:
            coerced[column] = str(value or "")
        else:
            coerced[column] = float(value or 0.0)
    for extra in ("bt_id", "equity_curve"):
        if extra in record and extra in allowed:
            coerced[extra] = record[extra]
    return ResultRow(**coerced)


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"

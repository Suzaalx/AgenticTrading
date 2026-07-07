"""Run-bundle markdown and CSV export helpers."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast

from sentinel.risk.audit import read_audit
from sentinel.store.db import sentinel_home

CsvExportKind = Literal["run", "backtest"]
CsvExportMode = Literal["auto", "run", "backtest"]


@dataclass(frozen=True)
class CsvExportResult:
    """Result metadata for a CSV export bundle."""

    kind: CsvExportKind
    export_id: str
    paths: list[Path]
    row_counts: dict[str, int]


def export_run_bundle(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    output_path: Path | None = None,
) -> Path:
    """Export reports, decision metadata, gate results, orders, and fills to markdown."""

    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        msg = f"unknown run_id {run_id!r}"
        raise ValueError(msg)
    reports = conn.execute(
        "SELECT * FROM reports WHERE run_id = ? ORDER BY created_at, agent",
        (run_id,),
    ).fetchall()
    orders = conn.execute(
        "SELECT * FROM orders WHERE run_id = ? ORDER BY created_at",
        (run_id,),
    ).fetchall()
    fills = conn.execute(
        "SELECT f.* FROM fills f JOIN orders o ON o.order_id = f.order_id "
        "WHERE o.run_id = ? ORDER BY f.ts",
        (run_id,),
    ).fetchall()
    journal = conn.execute("SELECT * FROM journal WHERE run_id = ?", (run_id,)).fetchone()
    gate_rows = _gate_rows_for_run(run_id)

    path = output_path or (sentinel_home() / "runs" / run_id / f"{run_id}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_markdown(
            run=dict(run),
            reports=[dict(row) for row in reports],
            orders=[dict(row) for row in orders],
            fills=[dict(row) for row in fills],
            journal=dict(journal) if journal is not None else None,
            gate_rows=gate_rows,
        ),
        encoding="utf-8",
    )
    return path


def export_csv_bundle(
    conn: sqlite3.Connection,
    export_id: str,
    *,
    output_dir: Path | None = None,
    kind: CsvExportMode = "auto",
) -> CsvExportResult:
    """Export a run or backtest bundle to one or more CSV files."""

    resolved_kind = _resolve_csv_kind(conn, export_id, kind)
    if resolved_kind == "run":
        return _export_run_csv(conn, export_id, output_dir)
    return _export_backtest_csv(conn, export_id, output_dir)


def _gate_rows_for_run(run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in read_audit():
        if record["kind"] != "gate_evaluated" or not isinstance(record["payload"], dict):
            continue
        payload = record["payload"]
        order = payload.get("order")
        if isinstance(order, dict) and order.get("run_id") == run_id:
            rows.append({"ts": record["ts"], **payload})
    return rows


def _resolve_csv_kind(conn: sqlite3.Connection, export_id: str, kind: CsvExportMode) -> CsvExportKind:
    if kind == "run":
        if _fetch_one(conn, "SELECT 1 FROM runs WHERE run_id = ?", export_id) is None:
            msg = f"unknown run_id {export_id!r}"
            raise ValueError(msg)
        return "run"
    if kind == "backtest":
        if _fetch_one(conn, "SELECT 1 FROM backtests WHERE bt_id = ?", export_id) is None:
            msg = f"unknown backtest id {export_id!r}"
            raise ValueError(msg)
        return "backtest"
    if _fetch_one(conn, "SELECT 1 FROM runs WHERE run_id = ?", export_id) is not None:
        return "run"
    if _fetch_one(conn, "SELECT 1 FROM backtests WHERE bt_id = ?", export_id) is not None:
        return "backtest"
    msg = f"unknown run or backtest id {export_id!r}"
    raise ValueError(msg)


def _export_run_csv(
    conn: sqlite3.Connection, run_id: str, output_path: Path | None
) -> CsvExportResult:
    run = _fetch_one(conn, "SELECT * FROM runs WHERE run_id = ?", run_id)
    if run is None:
        msg = f"unknown run_id {run_id!r}"
        raise ValueError(msg)
    journal = _fetch_one(conn, "SELECT * FROM journal WHERE run_id = ?", run_id)
    orders = _fetch_all(conn, "SELECT * FROM orders WHERE run_id = ? ORDER BY created_at", run_id)
    fills = _fetch_all(
        conn,
        "SELECT f.*, o.run_id, o.symbol, o.side, o.reason AS order_reason, o.status AS order_status "
        "FROM fills f JOIN orders o ON o.order_id = f.order_id "
        "WHERE o.run_id = ? ORDER BY f.ts",
        run_id,
    )

    target = output_path or (sentinel_home() / "runs" / run_id / "csv")
    if target.suffix.lower() == ".csv":
        journal_path = target
        paths = [journal_path]
        include_orders = False
    else:
        target.mkdir(parents=True, exist_ok=True)
        journal_path = target / f"{run_id}_journal.csv"
        paths = [journal_path, target / f"{run_id}_orders.csv", target / f"{run_id}_fills.csv"]
        include_orders = True

    row_counts: dict[str, int] = {}
    _write_csv(journal_path, _JOURNAL_COLUMNS, _journal_rows(run, journal))
    row_counts["journal"] = 1 if journal is not None else 0
    if include_orders:
        _write_csv(paths[1], _ORDER_COLUMNS, orders)
        _write_csv(paths[2], _FILL_COLUMNS, _fill_rows(fills))
        row_counts["orders"] = len(orders)
        row_counts["fills"] = len(fills)
    return CsvExportResult(kind="run", export_id=run_id, paths=paths, row_counts=row_counts)


def _export_backtest_csv(
    conn: sqlite3.Connection, bt_id: str, output_dir: Path | None
) -> CsvExportResult:
    row = _fetch_one(conn, "SELECT * FROM backtests WHERE bt_id = ?", bt_id)
    if row is None:
        msg = f"unknown backtest id {bt_id!r}"
        raise ValueError(msg)
    target = output_dir or (sentinel_home() / "runs" / bt_id / "csv")
    if target.suffix.lower() == ".csv":
        msg = "backtest CSV export requires a directory output path"
        raise ValueError(msg)
    target.mkdir(parents=True, exist_ok=True)

    config = _loads_object(row.get("config_json"), "config_json", bt_id)
    metrics = _loads_object(row.get("metrics_json"), "metrics_json", bt_id)
    equity_curve = _loads_list(row.get("equity_json"), "equity_json", bt_id)
    if not equity_curve:
        equity_curve = _as_dict_list(metrics.get("equity_curve"))
    trades = _as_dict_list(metrics.get("trades"))

    metrics_path = target / f"{bt_id}_metrics.csv"
    trades_path = target / f"{bt_id}_trades.csv"
    equity_path = target / f"{bt_id}_equity_curve.csv"
    _write_csv(metrics_path, _METRICS_COLUMNS, [_metrics_row(bt_id, row, config, metrics)])
    _write_csv(trades_path, _TRADE_COLUMNS, _trade_rows(bt_id, trades))
    _write_csv(equity_path, _EQUITY_COLUMNS, [{"bt_id": bt_id, **point} for point in equity_curve])
    return CsvExportResult(
        kind="backtest",
        export_id=bt_id,
        paths=[metrics_path, trades_path, equity_path],
        row_counts={"metrics": 1, "trades": len(trades), "equity_curve": len(equity_curve)},
    )


_JOURNAL_COLUMNS = [
    "run_id",
    "symbol",
    "date",
    "stance",
    "action",
    "conviction",
    "size",
    "thesis_summary",
    "invalidation",
    "horizon_end",
    "realized_ret",
    "bench_ret",
    "alpha_ret",
    "graded",
    "reflected_at",
    "run_status",
    "run_verdict",
    "run_cost_usd",
    "run_tokens",
]
_ORDER_COLUMNS = ["order_id", "run_id", "symbol", "side", "qty", "reason", "status", "created_at"]
_FILL_COLUMNS = [
    "order_id",
    "price",
    "qty",
    "slippage_usd",
    "commission_usd",
    "ts",
    "run_id",
    "symbol",
    "side",
    "order_reason",
    "order_status",
    "fill_notional_usd",
    "signed_cash_flow_usd",
    "execution_cost_usd",
]
_METRICS_COLUMNS = [
    "bt_id",
    "created_at",
    "symbol",
    "mode",
    "strategy",
    "start",
    "end",
    "cadence",
    "benchmark_symbol",
    "total_return",
    "annualized_return",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_drawdown_start",
    "max_drawdown_end",
    "win_rate",
    "profit_factor",
    "avg_win",
    "avg_loss",
    "exposure_pct",
    "turnover",
    "benchmark_return",
    "alpha",
    "bootstrap_sharpe_p05",
    "bootstrap_sharpe_p95",
    "bootstrap_max_drawdown_p05",
    "bootstrap_max_drawdown_p95",
    "config_extra_json",
]
_TRADE_COLUMNS = [
    "bt_id",
    "symbol",
    "entry_date",
    "entry_price",
    "exit_date",
    "exit_price",
    "qty",
    "pnl",
    "return",
    "bars_held",
    "entry_notional_usd",
    "exit_notional_usd",
    "gross_pnl_usd",
]
_EQUITY_COLUMNS = ["bt_id", "date", "equity", "cash", "position_qty", "close"]


def _fetch_one(conn: sqlite3.Connection, sql: str, value: str) -> dict[str, Any] | None:
    row = conn.execute(sql, (value,)).fetchone()
    return dict(row) if row is not None else None


def _fetch_all(conn: sqlite3.Connection, sql: str, value: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, (value,)).fetchall()]


def _journal_rows(run: dict[str, Any], journal: dict[str, Any] | None) -> list[dict[str, Any]]:
    if journal is None:
        return []
    realized = journal.get("realized_ret")
    benchmark = journal.get("bench_ret")
    alpha = None
    if realized is not None and benchmark is not None:
        alpha = Decimal(str(realized)) - Decimal(str(benchmark))
    return [
        {
            **journal,
            "alpha_ret": alpha,
            "run_status": run.get("status"),
            "run_verdict": run.get("verdict"),
            "run_cost_usd": run.get("cost_usd"),
            "run_tokens": run.get("tokens"),
        }
    ]


def _fill_rows(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fill in fills:
        price = _decimal_or_none(fill.get("price"))
        qty = _decimal_or_none(fill.get("qty"))
        slippage = _decimal_or_none(fill.get("slippage_usd")) or Decimal("0")
        commission = _decimal_or_none(fill.get("commission_usd")) or Decimal("0")
        notional = price * qty if price is not None and qty is not None else None
        side = str(fill.get("side") or "").lower()
        signed_cash_flow: Decimal | None = None
        if notional is not None:
            signed_cash_flow = -(notional + commission) if side == "buy" else notional - commission
        rows.append(
            {
                **fill,
                "fill_notional_usd": notional,
                "signed_cash_flow_usd": signed_cash_flow,
                "execution_cost_usd": slippage + commission,
            }
        )
    return rows


def _trade_rows(bt_id: str, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in trades:
        entry_price = _decimal_or_none(trade.get("entry_price"))
        exit_price = _decimal_or_none(trade.get("exit_price"))
        qty = _decimal_or_none(trade.get("qty"))
        entry_notional = entry_price * qty if entry_price is not None and qty is not None else None
        exit_notional = exit_price * qty if exit_price is not None and qty is not None else None
        rows.append(
            {
                "bt_id": bt_id,
                **trade,
                "entry_notional_usd": entry_notional,
                "exit_notional_usd": exit_notional,
                "gross_pnl_usd": trade.get("pnl"),
            }
        )
    return rows


_METRICS_CONFIG_KEYS = {
    "symbol",
    "mode",
    "strategy",
    "start",
    "end",
    "cadence",
    "benchmark_symbol",
    "csv_path",
    "data",
}


def _metrics_row(
    bt_id: str, row: dict[str, Any], config: dict[str, Any], metrics: dict[str, Any]
) -> dict[str, Any]:
    config_extra = {key: value for key, value in config.items() if key not in _METRICS_CONFIG_KEYS}
    return {
        "bt_id": bt_id,
        "created_at": row.get("created_at"),
        "symbol": config.get("symbol"),
        "mode": config.get("mode"),
        "strategy": config.get("strategy"),
        "start": config.get("start"),
        "end": config.get("end"),
        "cadence": config.get("cadence"),
        "config_extra_json": config_extra,
        **metrics,
    }


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in columns})


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            return value
        return format(decimal, "f") if decimal.is_finite() else ""
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, default=str, allow_nan=False)
    return str(value)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _loads_object(raw: object, field: str, export_id: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        msg = f"invalid backtest {field} for {export_id}"
        raise ValueError(msg) from exc
    if not isinstance(value, dict):
        msg = f"invalid backtest {field} for {export_id}"
        raise ValueError(msg)
    return cast(dict[str, Any], value)


def _loads_list(raw: object, field: str, export_id: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError as exc:
        msg = f"invalid backtest {field} for {export_id}"
        raise ValueError(msg) from exc
    if not isinstance(value, list):
        msg = f"invalid backtest {field} for {export_id}"
        raise ValueError(msg)
    return _as_dict_list(value)


def _as_dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]


def _render_markdown(
    *,
    run: dict[str, Any],
    reports: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    journal: dict[str, Any] | None,
    gate_rows: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Sentinel run {run['run_id']}",
        "",
        "## Summary",
        "",
        f"- Symbol: {run.get('symbol') or '—'}",
        f"- As of: {run.get('as_of') or '—'}",
        f"- Status: {run.get('status') or '—'}",
        f"- Action: {run.get('action') or '—'}",
        f"- Verdict: {run.get('verdict') or '—'}",
        f"- Cost: {run.get('cost_usd') or 0}",
        f"- Tokens: {run.get('tokens') or 0}",
        "",
        "## Gate result",
        "",
    ]
    if gate_rows:
        for gate in gate_rows:
            violations = gate.get("violations") or []
            lines.append(f"- {gate.get('ts')}: passed={gate.get('passed')}")
            if violations:
                lines.append("  - violations: " + json.dumps(violations, default=str))
    else:
        lines.append("No gate evaluation recorded.")
    lines.extend(["", "## Orders", ""])
    if orders:
        lines.append("| order_id | side | qty | reason | status | created_at |")
        lines.append("| --- | --- | ---: | --- | --- | --- |")
        for order in orders:
            lines.append(
                f"| {order.get('order_id')} | {order.get('side')} | {order.get('qty')} | "
                f"{order.get('reason')} | {order.get('status')} | {order.get('created_at')} |"
            )
    else:
        lines.append("No orders.")
    lines.extend(["", "## Fills", ""])
    if fills:
        lines.append("| order_id | price | qty | slippage | commission | ts |")
        lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
        for fill in fills:
            lines.append(
                f"| {fill.get('order_id')} | {fill.get('price')} | {fill.get('qty')} | "
                f"{fill.get('slippage_usd')} | {fill.get('commission_usd')} | {fill.get('ts')} |"
            )
    else:
        lines.append("No fills.")
    lines.extend(["", "## Reports", ""])
    if reports:
        for report in reports:
            lines.extend(
                [
                    f"### {report.get('agent') or 'agent'}",
                    "",
                    f"- Model: {report.get('model') or '—'}",
                    f"- Tokens: {report.get('tokens_in') or 0} in / {report.get('tokens_out') or 0} out",
                    f"- Cost: {report.get('cost_usd') or 0}",
                    "",
                    str(report.get("content") or ""),
                    "",
                ]
            )
    else:
        lines.append("No reports.")
    lines.extend(["", "## Outcome annotation", ""])
    if journal is not None:
        lines.append(
            f"realized={journal.get('realized_ret')} benchmark={journal.get('bench_ret')} "
            f"grade={journal.get('graded')} reflected_at={journal.get('reflected_at')}"
        )
    else:
        lines.append("No reflection outcome yet.")
    lines.append("")
    return "\n".join(lines)

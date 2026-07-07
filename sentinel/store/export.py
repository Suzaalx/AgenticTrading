"""Run-bundle markdown export helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sentinel.risk.audit import read_audit
from sentinel.store.db import sentinel_home


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

"""Typed helpers for LLM cost rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class CostRecord:
    ts: datetime
    run_id: str | None
    agent: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    latency_ms: int = 0


def insert_cost(conn: sqlite3.Connection, record: CostRecord) -> None:
    """Insert a cost row."""

    conn.execute(
        "INSERT INTO costs (ts, run_id, agent, model, tokens_in, tokens_out, cost_usd, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.ts.isoformat(),
            record.run_id,
            record.agent,
            record.model,
            record.tokens_in,
            record.tokens_out,
            str(record.cost_usd),
            int(record.latency_ms),
        ),
    )
    conn.commit()


def list_costs(conn: sqlite3.Connection, run_id: str | None = None) -> list[CostRecord]:
    """List costs, optionally filtered by run."""

    if run_id is None:
        rows = conn.execute("SELECT * FROM costs ORDER BY ts").fetchall()
    else:
        rows = conn.execute("SELECT * FROM costs WHERE run_id = ? ORDER BY ts", (run_id,)).fetchall()
    return [
        CostRecord(
            ts=datetime.fromisoformat(row["ts"]),
            run_id=row["run_id"],
            agent=row["agent"],
            model=row["model"],
            tokens_in=int(row["tokens_in"]),
            tokens_out=int(row["tokens_out"]),
            cost_usd=Decimal(str(row["cost_usd"])),
            latency_ms=int(row["latency_ms"] or 0),
        )
        for row in rows
    ]

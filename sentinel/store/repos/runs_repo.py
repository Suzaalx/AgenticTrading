"""Typed helpers for the runs table."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    symbol: str
    as_of: datetime
    mode: str
    status: str
    action: str | None = None
    verdict: str | None = None
    cost_usd: Decimal = Decimal("0")
    tokens: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None


def insert_run(conn: sqlite3.Connection, record: RunRecord) -> None:
    """Insert or replace a run row."""

    conn.execute(
        """INSERT OR REPLACE INTO runs
        (run_id, symbol, as_of, mode, status, action, verdict, cost_usd, tokens, created_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record.run_id,
            record.symbol,
            record.as_of.isoformat(),
            record.mode,
            record.status,
            record.action,
            record.verdict,
            str(record.cost_usd),
            record.tokens,
            (record.created_at or datetime.now()).isoformat(),
            record.finished_at.isoformat() if record.finished_at else None,
        ),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRecord | None:
    """Fetch one run by ID."""

    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunRecord(
        run_id=row["run_id"],
        symbol=row["symbol"],
        as_of=datetime.fromisoformat(row["as_of"]),
        mode=row["mode"],
        status=row["status"],
        action=row["action"],
        verdict=row["verdict"],
        cost_usd=Decimal(str(row["cost_usd"] or "0")),
        tokens=int(row["tokens"] or 0),
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
    )

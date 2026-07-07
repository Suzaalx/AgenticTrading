"""Typed helpers for orders and fills."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from sentinel.core.models import Fill, Order


def insert_order(conn: sqlite3.Connection, order: Order, status: str = "submitted") -> None:
    """Insert or replace an order."""

    conn.execute(
        """INSERT OR REPLACE INTO orders
        (order_id, run_id, symbol, side, qty, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order.order_id,
            order.run_id,
            order.symbol,
            order.side,
            str(order.qty),
            order.reason,
            status,
            order.created_at.isoformat(),
        ),
    )
    conn.commit()


def get_order(conn: sqlite3.Connection, order_id: str) -> Order | None:
    """Fetch one order by ID."""

    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    return Order(
        order_id=row["order_id"],
        run_id=row["run_id"],
        symbol=row["symbol"],
        side=row["side"],
        qty=Decimal(row["qty"]),
        type="market",
        reason=row["reason"],
        created_at=row["created_at"],
    )


def insert_fill(conn: sqlite3.Connection, fill: Fill) -> None:
    """Insert a fill row."""

    conn.execute(
        "INSERT INTO fills (order_id, price, qty, slippage_usd, commission_usd, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (
            fill.order_id,
            str(fill.price),
            str(fill.qty),
            str(fill.slippage_usd),
            str(fill.commission_usd),
            fill.ts.isoformat(),
        ),
    )
    conn.commit()

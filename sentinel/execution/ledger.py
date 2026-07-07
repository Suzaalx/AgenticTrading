"""SQLite order and fill ledger helpers for the execution layer."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from sentinel.core.models import Fill, Order
from sentinel.store.db import connect, run_migrations

OrderStatus = Literal["submitted", "filled", "rejected", "cancelled"]
OrderSide = Literal["buy", "sell"]
OrderReason = Literal["agent_decision", "stop_loss", "take_profit", "time_exit", "manual"]


def open_execution_connection(path: str | Path | None = None) -> sqlite3.Connection:
    """Open SQLite and ensure Sentinel tables exist."""

    conn = connect(path)
    run_migrations(conn)
    return conn


def append_order(
    conn: sqlite3.Connection, order: Order, status: OrderStatus = "submitted"
) -> None:
    """Append or replace an order row with Decimal values serialized as text."""

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


def update_order_status(conn: sqlite3.Connection, order_id: str, status: OrderStatus) -> None:
    """Update the persisted status for one order."""

    conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id))
    conn.commit()


def append_fill(conn: sqlite3.Connection, fill: Fill) -> None:
    """Append one fill row with Decimal values serialized as text."""

    conn.execute(
        """INSERT INTO fills
        (order_id, price, qty, slippage_usd, commission_usd, ts)
        VALUES (?, ?, ?, ?, ?, ?)""",
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


def record_fill(conn: sqlite3.Connection, order: Order, fill: Fill) -> None:
    """Persist a filled order and its fill atomically."""

    with conn:
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
                "filled",
                order.created_at.isoformat(),
            ),
        )
        conn.execute(
            """INSERT INTO fills
            (order_id, price, qty, slippage_usd, commission_usd, ts)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                fill.order_id,
                str(fill.price),
                str(fill.qty),
                str(fill.slippage_usd),
                str(fill.commission_usd),
                fill.ts.isoformat(),
            ),
        )


def get_order(conn: sqlite3.Connection, order_id: str) -> Order | None:
    """Fetch one order by ID."""

    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    return _order_from_row(row)


def list_orders(
    conn: sqlite3.Connection, *, symbol: str | None = None, limit: int = 100
) -> list[Order]:
    """List recent orders, optionally filtered by symbol."""

    if symbol is None:
        rows: Sequence[sqlite3.Row] = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return [_order_from_row(row) for row in rows]


def get_fills(conn: sqlite3.Connection, *, order_id: str | None = None) -> list[Fill]:
    """Fetch fills, optionally for one order."""

    if order_id is None:
        rows: Sequence[sqlite3.Row] = conn.execute(
            "SELECT * FROM fills ORDER BY ts ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fills WHERE order_id = ? ORDER BY ts ASC", (order_id,)
        ).fetchall()
    return [_fill_from_row(row) for row in rows]


def _order_from_row(row: sqlite3.Row) -> Order:
    return Order(
        order_id=cast(str, row["order_id"]),
        run_id=cast(str | None, row["run_id"]),
        symbol=cast(str, row["symbol"]),
        side=cast(OrderSide, row["side"]),
        qty=Decimal(cast(str, row["qty"])),
        type="market",
        reason=cast(OrderReason, row["reason"]),
        created_at=datetime.fromisoformat(cast(str, row["created_at"])),
    )


def _fill_from_row(row: sqlite3.Row) -> Fill:
    return Fill(
        order_id=cast(str, row["order_id"]),
        price=Decimal(cast(str, row["price"])),
        qty=Decimal(cast(str, row["qty"])),
        slippage_usd=Decimal(cast(str, row["slippage_usd"])),
        commission_usd=Decimal(cast(str, row["commission_usd"])),
        ts=datetime.fromisoformat(cast(str, row["ts"])),
    )

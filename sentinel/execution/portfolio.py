"""Paper portfolio accounting and persistence helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from sentinel.config.settings import load_settings
from sentinel.core.events import EquityUpdated
from sentinel.core.models import Fill, Order, Portfolio, Position
from sentinel.store.db import connect, run_migrations


@dataclass(frozen=True)
class EquitySnapshot:
    """One persisted equity-curve point."""

    ts: datetime
    equity: Decimal
    cash: Decimal
    day_pnl: Decimal


def apply_fill(portfolio: Portfolio, order: Order, fill: Fill) -> tuple[Portfolio, Decimal]:
    """Apply a fill to a portfolio and return the updated state plus realized P&L.

    Buy commissions are returned as negative realized P&L so a complete round trip sums to
    quote price delta minus all slippage and commissions while keeping avg_cost as fill price.
    """

    if order.order_id != fill.order_id:
        msg = "fill.order_id must match order.order_id"
        raise ValueError(msg)
    if fill.qty <= Decimal("0"):
        msg = "fill quantity must be positive"
        raise ValueError(msg)

    positions = [position.model_copy() for position in portfolio.positions]
    position_index = _find_position_index(positions, order.symbol)

    if order.side == "buy":
        notional = fill.price * fill.qty
        new_cash = portfolio.cash - notional - fill.commission_usd
        if new_cash < Decimal("0"):
            msg = "buy fill would make cash negative"
            raise ValueError(msg)
        if position_index is None:
            positions.append(
                Position(
                    symbol=order.symbol,
                    qty=fill.qty,
                    avg_cost=fill.price,
                    stop_loss_pct=None,
                    take_profit_pct=None,
                    opened_at=fill.ts,
                    horizon_days=None,
                    source_run_id=order.run_id,
                )
            )
        else:
            existing = positions[position_index]
            combined_qty = existing.qty + fill.qty
            combined_cost = (existing.qty * existing.avg_cost) + (fill.qty * fill.price)
            positions[position_index] = existing.model_copy(
                update={"qty": combined_qty, "avg_cost": combined_cost / combined_qty}
            )
        realized_pnl = -fill.commission_usd
        return (
            Portfolio(
                cash=new_cash,
                positions=positions,
                day_pnl=portfolio.day_pnl + realized_pnl,
            ),
            realized_pnl,
        )

    if position_index is None:
        msg = f"cannot sell {order.symbol} without an open position"
        raise ValueError(msg)
    existing = positions[position_index]
    if fill.qty > existing.qty:
        msg = f"cannot sell {fill.qty} {order.symbol}; only {existing.qty} available"
        raise ValueError(msg)

    proceeds = fill.price * fill.qty
    new_cash = portfolio.cash + proceeds - fill.commission_usd
    realized_pnl = (fill.price - existing.avg_cost) * fill.qty - fill.commission_usd
    remaining_qty = existing.qty - fill.qty
    if remaining_qty == Decimal("0"):
        del positions[position_index]
    else:
        positions[position_index] = existing.model_copy(update={"qty": remaining_qty})
    return (
        Portfolio(
            cash=new_cash,
            positions=positions,
            day_pnl=portfolio.day_pnl + realized_pnl,
        ),
        realized_pnl,
    )


def unrealized_pnl(portfolio: Portfolio, marks: dict[str, Decimal]) -> Decimal:
    """Return mark-to-market P&L for open positions."""

    return sum(
        (
            position.qty * (marks.get(position.symbol, position.avg_cost) - position.avg_cost)
            for position in portfolio.positions
        ),
        Decimal("0"),
    )


def open_portfolio_connection(path: str | Path | None = None) -> sqlite3.Connection:
    """Open SQLite and ensure tables exist."""

    conn = connect(path)
    run_migrations(conn)
    return conn


def load_positions(conn: sqlite3.Connection) -> list[Position]:
    """Load all open positions from SQLite."""

    rows = conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
    return [
        Position(
            symbol=cast(str, row["symbol"]),
            qty=Decimal(cast(str, row["qty"])),
            avg_cost=Decimal(cast(str, row["avg_cost"])),
            stop_loss_pct=cast(float | None, row["stop_pct"]),
            take_profit_pct=cast(float | None, row["tp_pct"]),
            horizon_days=cast(int | None, row["horizon_days"]),
            opened_at=datetime.fromisoformat(cast(str, row["opened_at"])),
            source_run_id=cast(str | None, row["source_run_id"]),
        )
        for row in rows
    ]


def save_positions(conn: sqlite3.Connection, portfolio: Portfolio) -> None:
    """Persist the portfolio's open positions, removing positions closed in memory."""

    symbols = [position.symbol for position in portfolio.positions]
    with conn:
        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            conn.execute(f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})", symbols)
        else:
            conn.execute("DELETE FROM positions")
        for position in portfolio.positions:
            conn.execute(
                """INSERT OR REPLACE INTO positions
                (symbol, qty, avg_cost, stop_pct, tp_pct, horizon_days, opened_at, source_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position.symbol,
                    str(position.qty),
                    str(position.avg_cost),
                    position.stop_loss_pct,
                    position.take_profit_pct,
                    position.horizon_days,
                    position.opened_at.isoformat(),
                    position.source_run_id,
                ),
            )


def load_portfolio(
    conn: sqlite3.Connection, *, starting_cash: Decimal | None = None
) -> Portfolio:
    """Read current portfolio from persisted positions and the latest cash snapshot."""

    latest = conn.execute(
        "SELECT cash, day_pnl FROM equity_curve ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        cash = starting_cash if starting_cash is not None else _settings_starting_cash()
        day_pnl = Decimal("0")
    else:
        cash = Decimal(str(latest["cash"]))
        day_pnl = Decimal(str(latest["day_pnl"]))
    return Portfolio(cash=cash, positions=load_positions(conn), day_pnl=day_pnl)


def append_equity_snapshot(
    conn: sqlite3.Connection,
    portfolio: Portfolio,
    *,
    marks: dict[str, Decimal] | None = None,
    ts: datetime | None = None,
) -> EquitySnapshot:
    """Append and return an equity-curve snapshot."""

    snapshot_ts = ts or datetime.now(UTC)
    equity = portfolio.marked_equity(marks or {})
    snapshot = EquitySnapshot(
        ts=snapshot_ts,
        equity=equity,
        cash=portfolio.cash,
        day_pnl=portfolio.day_pnl,
    )
    conn.execute(
        "INSERT INTO equity_curve (ts, equity, cash, day_pnl) VALUES (?, ?, ?, ?)",
        (
            snapshot.ts.isoformat(),
            str(snapshot.equity),
            str(snapshot.cash),
            str(snapshot.day_pnl),
        ),
    )
    conn.commit()
    return snapshot


class PortfolioAccounting:
    """Stateful portfolio accounting backed by Sentinel SQLite."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        starting_cash: Decimal | None = None,
    ) -> None:
        self._db_path = db_path
        self._starting_cash = starting_cash

    def load(self) -> Portfolio:
        """Load the current persisted portfolio."""

        with open_portfolio_connection(self._db_path) as conn:
            return load_portfolio(conn, starting_cash=self._starting_cash)

    def apply_fill(
        self,
        order: Order,
        fill: Fill,
        *,
        marks: dict[str, Decimal] | None = None,
    ) -> tuple[Portfolio, Decimal, EquitySnapshot]:
        """Apply, persist, and snapshot a fill."""

        with open_portfolio_connection(self._db_path) as conn:
            portfolio = load_portfolio(conn, starting_cash=self._starting_cash)
            updated, realized_pnl = apply_fill(portfolio, order, fill)
            save_positions(conn, updated)
            snapshot = append_equity_snapshot(conn, updated, marks=marks)
        return updated, realized_pnl, snapshot

    def snapshot(
        self,
        portfolio: Portfolio | None = None,
        *,
        marks: dict[str, Decimal] | None = None,
    ) -> EquitySnapshot:
        """Persist a monitor-tick equity snapshot."""

        with open_portfolio_connection(self._db_path) as conn:
            current = portfolio if portfolio is not None else load_portfolio(
                conn, starting_cash=self._starting_cash
            )
            return append_equity_snapshot(conn, current, marks=marks)

    @staticmethod
    def equity_event(snapshot: EquitySnapshot) -> EquityUpdated:
        """Convert a snapshot to the core event consumed by monitors and the TUI."""

        return EquityUpdated(
            ts=snapshot.ts,
            equity=snapshot.equity,
            cash=snapshot.cash,
            day_pnl=snapshot.day_pnl,
        )


def _find_position_index(positions: list[Position], symbol: str) -> int | None:
    for index, position in enumerate(positions):
        if position.symbol == symbol:
            return index
    return None


def _settings_starting_cash() -> Decimal:
    return Decimal(str(load_settings().execution.starting_cash_usd))

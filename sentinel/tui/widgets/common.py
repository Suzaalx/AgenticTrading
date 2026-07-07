"""Reusable TUI helpers and lightweight widgets."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from textual_plotext import PlotextPlot

from sentinel.store.db import connect

ZERO = Decimal("0")


def decimal_from(value: object, default: Decimal = ZERO) -> Decimal:
    """Convert SQLite/Pydantic scalar values to ``Decimal`` defensively."""

    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def money(value: Decimal | float | int | str | None) -> str:
    """Format a dollar amount."""

    amount = decimal_from(value)
    return f"${amount:,.2f}"


def signed_money(value: Decimal | float | int | str | None) -> str:
    """Format a dollar amount with an explicit sign."""

    amount = decimal_from(value)
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def signed_percent(value: Decimal | float | int | str | None, *, already_percent: bool = True) -> str:
    """Format a percentage with an explicit sign."""

    amount = decimal_from(value)
    if not already_percent:
        amount *= Decimal("100")
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{abs(amount):.2f}%"


def compact_time(ts: datetime | None = None) -> str:
    """Return a compact timestamp for live feeds."""

    value = ts or datetime.now(UTC)
    return value.astimezone().strftime("%H:%M:%S")


def fetch_rows(sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
    """Run a read-only store query, returning an empty set if tables are absent."""

    try:
        with connect() as conn:
            return list(conn.execute(sql, tuple(params)))
    except sqlite3.OperationalError:
        return []


def latest_equity_row() -> sqlite3.Row | None:
    """Return the newest equity snapshot."""

    rows = fetch_rows(
        "SELECT ts, equity, cash, day_pnl FROM equity_curve ORDER BY ts DESC LIMIT 1"
    )
    return rows[0] if rows else None


def append_limited(items: list[str], item: str, *, limit: int = 80) -> None:
    """Append to a display buffer while keeping the newest ``limit`` rows."""

    items.append(item)
    if len(items) > limit:
        del items[: len(items) - limit]


def row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read a sqlite row key that may be absent in older test fixtures."""

    keys = row.keys()
    return row[key] if key in keys else default


class EquityPlot(PlotextPlot):
    """Small wrapper around textual-plotext for equity/drawdown series."""

    def update_series(self, values: Iterable[float], *, title: str) -> None:
        """Render a line plot, or a titled empty plot when no values exist."""

        series = list(values)
        self.plt.clear_figure()
        self.plt.title(title)
        if series:
            self.plt.plot(list(range(1, len(series) + 1)), series)
        else:
            self.plt.text("No data", 1, 1)
        self.refresh()

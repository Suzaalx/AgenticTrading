"""Monthly LLM budget checks."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal


class BudgetExceeded(RuntimeError):
    """Raised when month-to-date LLM spend is at or above the configured budget."""

    def __init__(self, spent: Decimal, budget: Decimal) -> None:
        super().__init__(f"LLM monthly budget exceeded: spent ${spent}, budget ${budget}")
        self.spent = spent
        self.budget = budget


def month_to_date_cost(conn: sqlite3.Connection) -> Decimal:
    """Return the current UTC month-to-date LLM cost."""

    month_prefix = datetime.now(UTC).strftime("%Y-%m")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM costs WHERE substr(ts, 1, 7) = ?",
        (month_prefix,),
    ).fetchone()
    return Decimal(str(row["total"] if row is not None else "0"))


def assert_within_budget(conn: sqlite3.Connection, budget: Decimal | float | str) -> None:
    """Raise BudgetExceeded if the configured monthly budget has already been spent."""

    budget_decimal = Decimal(str(budget))
    spent = month_to_date_cost(conn)
    if spent >= budget_decimal:
        raise BudgetExceeded(spent=spent, budget=budget_decimal)

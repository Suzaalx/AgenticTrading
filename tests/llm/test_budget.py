from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.llm.budget import BudgetExceeded, assert_within_budget, month_to_date_cost
from sentinel.llm.cost import record_cost
from sentinel.store.db import connect, run_migrations


def test_over_budget_raises_budget_exceeded(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    record_cost(
        conn,
        run_id="run_1",
        agent="market_analyst",
        model="claude-sonnet-5",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
    )

    assert month_to_date_cost(conn) == Decimal("18")
    with pytest.raises(BudgetExceeded):
        assert_within_budget(conn, Decimal("1"))

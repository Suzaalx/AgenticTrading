from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sentinel.core.models import Order
from sentinel.store.db import connect, run_migrations, sentinel_home
from sentinel.store.migrations import EXPECTED_TABLES
from sentinel.store.repos.orders_repo import get_order, insert_order
from sentinel.store.repos.runs_repo import RunRecord, get_run, insert_run


def test_migrations_create_all_tables() -> None:
    conn = connect(sentinel_home() / "test.db")
    try:
        run_migrations(conn)
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','virtual')")
        }
        assert set(EXPECTED_TABLES).issubset(names)
    finally:
        conn.close()


def test_repo_insert_select_round_trip() -> None:
    conn = connect(sentinel_home() / "repo.db")
    try:
        run_migrations(conn)
        now = datetime.now(UTC)
        run = RunRecord(run_id="01J", symbol="NVDA", as_of=now, mode="decision", status="pending")
        insert_run(conn, run)
        fetched_run = get_run(conn, "01J")
        assert fetched_run is not None
        assert fetched_run.run_id == run.run_id
        assert fetched_run.symbol == run.symbol
        order = Order(
            order_id="01O",
            run_id="01J",
            symbol="NVDA",
            side="buy",
            qty=Decimal("1"),
            type="market",
            reason="agent_decision",
            created_at=now,
        )
        insert_order(conn, order)
        assert get_order(conn, "01O") == order
    finally:
        conn.close()

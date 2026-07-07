from __future__ import annotations

from decimal import Decimal

from sentinel.llm.cost import cost_usd, record_cost
from sentinel.store.db import connect, run_migrations


def test_cost_metering_math_for_known_models() -> None:
    assert cost_usd("gpt-5.4-mini", 1_000_000, 1_000_000) == Decimal("2.25000000")
    assert cost_usd("claude-sonnet-5", 2_000_000, 1_000_000) == Decimal("21.00000000")


def test_record_cost_inserts_row(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)

    written = record_cost(
        conn,
        run_id="run_1",
        agent="market_analyst",
        model="gpt-5.4-mini",
        tokens_in=1000,
        tokens_out=500,
    )

    row = conn.execute("SELECT * FROM costs WHERE run_id = ?", ("run_1",)).fetchone()
    assert Decimal(str(row["cost_usd"])) == written
    assert row["agent"] == "market_analyst"

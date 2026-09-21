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


def test_free_tier_models_record_tokens_and_latency_at_zero_cost(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)

    written = record_cost(
        conn,
        run_id="run_free",
        agent="trader",
        model="openai/gpt-oss-120b",
        tokens_in=2_000,
        tokens_out=300,
        latency_ms=842,
    )

    row = conn.execute("SELECT * FROM costs WHERE run_id = ?", ("run_free",)).fetchone()
    assert written == Decimal("0")
    assert (row["tokens_in"], row["tokens_out"], row["latency_ms"]) == (2_000, 300, 842)


def test_migrations_add_latency_column_to_pre_existing_costs_table(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "old.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE costs (ts TEXT, run_id TEXT, agent TEXT, model TEXT, tokens_in INT, "
        "tokens_out INT, cost_usd REAL)"
    )
    legacy.execute("INSERT INTO costs VALUES ('2026-01-01T00:00:00', 'r', 'a', 'm', 1, 1, 0.0)")
    legacy.commit()
    legacy.close()

    conn = connect(db_path)
    run_migrations(conn)
    run_migrations(conn)  # idempotent

    columns = {row[1] for row in conn.execute("PRAGMA table_info(costs)")}
    assert "latency_ms" in columns
    from sentinel.store.repos.costs_repo import list_costs

    assert list_costs(conn, "r")[0].latency_ms == 0

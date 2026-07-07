from __future__ import annotations

from sentinel.store.db import connect, run_migrations


def test_option_migrations_are_idempotent(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    try:
        run_migrations(conn)
        run_migrations(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
            )
        }
    finally:
        conn.close()

    assert {
        "option_positions",
        "option_chain_meta",
        "iv_history",
        "live_orders",
        "reconciliations",
    }.issubset(names)

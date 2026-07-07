from __future__ import annotations

import csv
import json
from pathlib import Path

from sentinel.memory.journal import ensure_journal_schema
from sentinel.store.db import connect, run_migrations
from sentinel.store.export import export_csv_bundle


def test_export_run_with_options_and_live_venue_round_trips(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    try:
        run_migrations(conn)
        ensure_journal_schema(conn)
        conn.execute(
            """INSERT INTO runs
            (run_id, symbol, as_of, mode, status, action, verdict, cost_usd, tokens, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-opt", "NVDA", "2026-07-07", "decision", "completed", "OPEN_OPTION", "APPROVE", 0, 0, "t0", "t1"),
        )
        conn.execute(
            """INSERT INTO journal
            (run_id, symbol, date, stance, action, conviction, size, thesis_summary, invalidation,
             horizon_end, realized_ret, bench_ret, graded, reflected_at, strategy, venue)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "run-opt",
                "NVDA",
                "2026-07-07",
                "bullish",
                "OPEN_OPTION",
                70,
                0.1,
                "Defined-risk call",
                "Break support",
                "2026-08-21",
                0.03,
                0.01,
                "good_call",
                "t2",
                "long_call",
                "robinhood_agentic",
            ),
        )
        leg = {
            "contract": {
                "contract_symbol": "NVDA260821C00100000",
                "underlying": "NVDA",
                "kind": "call",
                "strike": "100",
                "expiry": "2026-08-21",
                "multiplier": 100,
            },
            "side": "buy",
            "contracts": 1,
            "limit_price": "2.50",
        }
        conn.execute(
            "INSERT INTO option_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "pos-1",
                "NVDA",
                "long_call",
                json.dumps([leg]),
                "250",
                "250",
                "0",
                "t0",
                "2026-08-21",
                30,
                None,
                None,
                "run-opt",
                "robinhood_agentic",
            ),
        )
        raw_order = {"order": {"run_id": "run-opt", "order_id": "ord-live", "venue": "robinhood_agentic"}}
        conn.execute(
            "INSERT INTO live_orders VALUES (?,?,?,?,?,?,?)",
            ("client-1", "broker-1", "robinhood_agentic", "working", "t0", "t1", json.dumps(raw_order)),
        )
        conn.commit()

        result = export_csv_bundle(conn, "run-opt", output_dir=tmp_path / "csv")

        assert result.row_counts["option_positions"] == 1
        assert result.row_counts["live_orders"] == 1
        journal = _read_csv(tmp_path / "csv" / "run-opt_journal.csv")[0]
        assert journal["venue"] == "robinhood_agentic"
        assert journal["option_strategy"] == "long_call"
        option_row = _read_csv(tmp_path / "csv" / "run-opt_option_positions.csv")[0]
        assert option_row["venue"] == "robinhood_agentic"
        live_row = _read_csv(tmp_path / "csv" / "run-opt_live_orders.csv")[0]
        assert live_row["venue"] == "robinhood_agentic"
    finally:
        conn.close()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))

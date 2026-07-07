from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sentinel.__main__ import app
from sentinel.memory.journal import ensure_journal_schema
from sentinel.store.db import connect, default_db_path, run_migrations
from sentinel.store.export import export_csv_bundle


def test_export_run_csv_bundle_writes_journal_orders_and_fills(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    try:
        run_migrations(conn)
        ensure_journal_schema(conn)
        conn.execute(
            """INSERT INTO runs
            (run_id, symbol, as_of, mode, status, action, verdict, cost_usd, tokens, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-1", "NVDA", "2026-01-02", "decision", "complete", "BUY", "approved", 1.25, 42, "t0", "t1"),
        )
        conn.execute(
            """INSERT INTO journal
            (run_id, symbol, date, stance, action, conviction, size, thesis_summary, invalidation,
             horizon_end, realized_ret, bench_ret, graded, reflected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "run-1",
                "NVDA",
                "2026-01-02",
                "bullish",
                "BUY",
                80,
                0.25,
                "Breakout",
                "Below support",
                "2026-01-09",
                0.12,
                0.02,
                "good_call",
                "2026-01-10T00:00:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO orders
            (order_id, run_id, symbol, side, qty, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("ord-1", "run-1", "NVDA", "buy", "1.2300", "agent_decision", "filled", "t0"),
        )
        conn.execute(
            """INSERT INTO fills
            (order_id, price, qty, slippage_usd, commission_usd, ts)
            VALUES (?, ?, ?, ?, ?, ?)""",
            ("ord-1", "100.50", "1.2300", "0.05", "0", "t1"),
        )
        conn.commit()

        result = export_csv_bundle(conn, "run-1", output_dir=tmp_path / "csv")

        assert result.kind == "run"
        assert result.row_counts == {"journal": 1, "orders": 1, "fills": 1}
        journal_rows = _read_csv(tmp_path / "csv" / "run-1_journal.csv")
        assert journal_rows[0]["run_id"] == "run-1"
        assert journal_rows[0]["alpha_ret"] == "0.10"
        assert journal_rows[0]["run_verdict"] == "approved"
        assert _read_csv(tmp_path / "csv" / "run-1_orders.csv")[0]["qty"] == "1.2300"
        assert _read_csv(tmp_path / "csv" / "run-1_fills.csv")[0]["price"] == "100.50"
    finally:
        conn.close()


def test_export_backtest_csv_bundle_writes_metrics_trades_and_equity(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    try:
        run_migrations(conn)
        config = {
            "symbol": "SPY",
            "mode": "rule",
            "strategy": "sma_cross",
            "start": "2025-01-01",
            "end": "2025-02-01",
            "cadence": "daily",
        }
        metrics = {
            "benchmark_symbol": "SPY",
            "total_return": 0.1,
            "annualized_return": 0.2,
            "sharpe": 1.5,
            "sortino": 2.0,
            "max_drawdown": -0.05,
            "max_drawdown_start": "2025-01-10",
            "max_drawdown_end": "2025-01-12",
            "win_rate": 0.6,
            "profit_factor": 1.8,
            "avg_win": 0.03,
            "avg_loss": -0.01,
            "exposure_pct": 0.9,
            "turnover": 1.2,
            "benchmark_return": 0.08,
            "alpha": 0.02,
            "bootstrap_sharpe_p05": float("nan"),
            "bootstrap_sharpe_p95": 2.2,
            "bootstrap_max_drawdown_p05": -0.08,
            "bootstrap_max_drawdown_p95": -0.02,
            "trades": [
                {
                    "symbol": "SPY",
                    "entry_date": "2025-01-02",
                    "entry_price": 100,
                    "exit_date": "2025-01-05",
                    "exit_price": 105,
                    "qty": 1,
                    "pnl": 5,
                    "return": 0.05,
                    "bars_held": 3,
                }
            ],
        }
        equity = [{"date": "2025-01-02", "equity": 10000, "cash": 9900, "position_qty": 1, "close": 100}]
        conn.execute(
            "INSERT INTO backtests (bt_id, config_json, metrics_json, equity_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("bt-1", json.dumps(config), json.dumps(metrics, allow_nan=True), json.dumps(equity), "created"),
        )
        conn.commit()

        result = export_csv_bundle(conn, "bt-1", output_dir=tmp_path / "bt", kind="backtest")

        assert result.kind == "backtest"
        assert result.row_counts == {"metrics": 1, "trades": 1, "equity_curve": 1}
        metrics_rows = _read_csv(tmp_path / "bt" / "bt-1_metrics.csv")
        assert metrics_rows[0]["symbol"] == "SPY"
        assert metrics_rows[0]["bootstrap_sharpe_p05"] == ""
        assert _read_csv(tmp_path / "bt" / "bt-1_trades.csv")[0]["pnl"] == "5"
        assert _read_csv(tmp_path / "bt" / "bt-1_equity_curve.csv")[0]["equity"] == "10000"
    finally:
        conn.close()


def test_export_backtest_csv_rejects_invalid_metrics_json(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO backtests (bt_id, config_json, metrics_json, equity_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("bt-bad", "{}", "{not-json", "[]", "created"),
        )
        conn.commit()

        with pytest.raises(ValueError, match="invalid backtest metrics_json for bt-bad"):
            export_csv_bundle(conn, "bt-bad", output_dir=tmp_path / "bt", kind="backtest")
    finally:
        conn.close()


def test_cli_export_csv_outputs_machine_readable_paths(tmp_path: Path) -> None:
    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        conn.execute(
            """INSERT INTO runs
            (run_id, symbol, as_of, mode, status, action, verdict, cost_usd, tokens, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-cli", "MSFT", "2026-01-02", "decision", "complete", "HOLD", "vetoed", 0, 0, "t0", "t1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(
        app, ["export", "run-cli", "--csv", "--output", str(tmp_path / "one.csv"), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "run-cli"
    assert payload["kind"] == "run"
    assert payload["row_counts"] == {"journal": 0}
    assert Path(payload["paths"][0]).name == "one.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from sentinel.core.bus import EventBus
from sentinel.core.events import LogLine
from sentinel.risk.audit import append_audit
from sentinel.store.db import connect, run_migrations
from sentinel.tui.app import SentinelApp


def seed_phase7_store() -> None:
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("run-1", "NVDA", now, "decision", "completed", "BUY", "APPROVE", 0.25, 1000, now, now),
        )
        conn.execute(
            "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-1", "market", "quick", "## Market\nUptrend", "{}", 10, 20, 0.01, 100, now),
        )
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            ("ord-1", "run-1", "NVDA", "buy", "3", "agent_decision", "filled", now),
        )
        conn.execute(
            "INSERT INTO journal VALUES (?,?,?,?,?,?)",
            ("run-1", "2026-07-10", 0.042, 0.011, "good_call", now),
        )
        conn.execute(
            "INSERT INTO lessons VALUES (?,?,?,?,?,?,?)",
            (
                "lesson-1",
                "NVDA",
                "momentum",
                "Momentum breakout worked.",
                "Respect confirmed momentum breakouts.",
                "good_call",
                now,
            ),
        )
        metrics = {
            "total_return": 0.12,
            "annualized_return": 0.20,
            "sharpe": 1.4,
            "max_drawdown": -0.05,
            "win_rate": 0.6,
            "profit_factor": 1.8,
            "benchmark_symbol": "SPY",
            "benchmark_return": 0.08,
            "alpha": 0.04,
            "trades": [{"symbol": "NVDA", "entry_date": "2026-01-01", "exit_date": "2026-02-01", "pnl": 123.0}],
        }
        equity = [{"date": "2026-01-01", "equity": 10000}, {"date": "2026-02-01", "equity": 11200}]
        conn.execute(
            "INSERT INTO backtests VALUES (?,?,?,?,?)",
            (
                "bt-123456",
                json.dumps({"symbol": "NVDA", "mode": "rule"}),
                json.dumps(metrics),
                json.dumps(equity),
                now,
            ),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_history_backtest_memory_logs_mount_render_and_keys() -> None:
    seed_phase7_store()
    append_audit("order_submitted", {"order_id": "ord-1"}, actor="test")
    bus = EventBus()
    app = SentinelApp(event_bus=bus)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one("#main-tabs", TabbedContent)

        await pilot.press("f4")
        assert tabs.active == "history"
        assert app.query_one("#history-runs", DataTable).row_count == 1
        detail = str(app.query_one("#history-detail", Static).content)
        assert "Outcome" in detail
        assert "good_call" in detail

        await pilot.press("f5")
        assert app.query_one("#backtest-past-results", DataTable).row_count == 1
        results = str(app.query_one("#backtest-results", Static).content)
        assert "Metrics" in results
        assert "Trades" in results

        await pilot.press("f6")
        assert app.query_one("#memory-lessons", DataTable).row_count == 1
        assert "Respect confirmed momentum" in str(app.query_one("#memory-detail", Static).content)
        await pilot.press("/")

        await pilot.press("f7")
        logs = str(app.query_one("#logs-tail", Static).content)
        assert "order_submitted" in logs
        await bus.publish(LogLine(level="warning", message="phase7 warning"))
        await pilot.pause()
        assert "phase7 warning" in str(app.query_one("#logs-tail", Static).content)
        await pilot.press("space")
        assert "[PAUSED]" in str(app.query_one("#logs-tail", Static).content)

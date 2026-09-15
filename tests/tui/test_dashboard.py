from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.widgets import DataTable, Static

from sentinel.store.db import connect, run_migrations
from sentinel.tui.app import SentinelApp


def seed_dashboard_store() -> None:
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
            ("NVDA", "4", "168.20", -8.0, 12.0, "2026-07-01T13:30:00Z", 30, "01JRUN"),
        )
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
            ("AAPL", "8", "212.10", -8.0, 10.0, "2026-07-02T13:30:00Z", 20, "01JAPL"),
        )
        conn.execute(
            "INSERT INTO equity_curve VALUES (?,?,?,?)",
            (datetime.now(UTC).isoformat(), 12345.67, 4200.00, 123.45),
        )
        conn.execute(
            "INSERT INTO runs (run_id, symbol, as_of, mode, status, action, verdict, cost_usd, tokens, "
            "created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "01JRUN",
                "NVDA",
                "2026-07-06T13:30:00Z",
                "decision",
                "completed",
                "BUY",
                "APPROVE",
                0.31,
                2113,
                "2026-07-06T13:30:00Z",
                "2026-07-06T13:32:00Z",
            ),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_dashboard_renders_seeded_portfolio_and_signed_pnl() -> None:
    seed_dashboard_store()
    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        summary = str(app.query_one("#dashboard-summary", Static).content)
        assert "Equity      $12,345.67" in summary
        assert "Cash        $4,200.00" in summary
        assert "Day P&L     +$123.45" in summary

        table = app.query_one("#dashboard-positions", DataTable)
        assert table.row_count == 2
        assert table.get_row_at(0)[0] == "AAPL"
        assert table.get_row_at(1)[0] == "NVDA"

        decisions = str(app.query_one("#dashboard-decisions", Static).content)
        assert "NVDA BUY APPROVE $0.31" in decisions

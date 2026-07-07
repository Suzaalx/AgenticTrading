from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.widgets import DataTable

from sentinel.core.commands import Depth
from sentinel.store.db import connect, run_migrations
from sentinel.tui.app import SentinelApp


class FakeCommandService:
    async def start_run(self, symbol: str, date: object, depth: Depth) -> str:
        return f"run-{symbol}-{depth}"

    async def cancel_run(self, run_id: str) -> None:
        return None

    async def toggle_kill(self, on: bool) -> None:
        return None

    async def close_position(self, symbol: str) -> None:
        return None

    async def start_backtest(self, config: dict[str, Any]) -> str:
        return "bt-1"

    async def reflect_now(self) -> None:
        return None


def seed_round_trip_trade() -> None:
    opened = datetime(2026, 7, 7, 14, tzinfo=UTC)
    closed = opened + timedelta(hours=1)
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            ("buy-1", "run-1", "AAPL", "buy", "10", "agent_decision", "filled", opened.isoformat()),
        )
        conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?)",
            ("buy-1", "100", "10", "0", "1", opened.isoformat()),
        )
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            ("sell-1", "run-1", "AAPL", "sell", "10", "manual", "filled", closed.isoformat()),
        )
        conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?)",
            ("sell-1", "110", "10", "0", "1", closed.isoformat()),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_closed_trade_table_renders_realized_pnl_for_round_trip() -> None:
    seed_round_trip_trade()
    app = SentinelApp(command_service=FakeCommandService())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")

        closed = app.query_one("#portfolio-closed-trades", DataTable)
        assert closed.row_count == 2
        sell_row = closed.get_row_at(0)
        assert sell_row[0] == "sell-1"
        assert sell_row[1] == "AAPL"
        assert sell_row[5] == "+$98.00"

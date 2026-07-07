from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from textual.widgets import DataTable, Static

from sentinel.core.commands import Depth
from sentinel.store.db import connect, run_migrations
from sentinel.tui.app import SentinelApp


class FakeCommandService:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def start_run(self, symbol: str, date: date | None, depth: Depth) -> str:
        return f"run-{symbol}-{depth}"

    async def cancel_run(self, run_id: str) -> None:
        return None

    async def toggle_kill(self, on: bool) -> None:
        return None

    async def close_position(self, symbol: str) -> None:
        self.closed.append(symbol)

    async def start_backtest(self, config: dict[str, Any]) -> str:
        return "bt-1"

    async def reflect_now(self) -> None:
        return None


def seed_portfolio_store() -> None:
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
            ("NVDA", "4", "168.20", -8.0, 12.0, now, 30, "01JRUN"),
        )
        conn.executemany(
            "INSERT INTO equity_curve VALUES (?,?,?,?)",
            [
                ("2026-07-01T13:30:00Z", 10000.0, 4000.0, 0.0),
                ("2026-07-02T13:30:00Z", 10200.0, 4100.0, 200.0),
                ("2026-07-03T13:30:00Z", 10100.0, 4050.0, -100.0),
            ],
        )
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            ("ord-closed", "01JRUN", "AAPL", "sell", "8", "manual", "filled", now),
        )
        conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?)",
            ("ord-closed", "214.05", "8", "0", "0", now),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_portfolio_renders_seeded_state_and_manual_close_command() -> None:
    seed_portfolio_store()
    commands = FakeCommandService()
    app = SentinelApp(command_service=commands)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")

        stats = str(app.query_one("#portfolio-stats", Static).content)
        assert "Equity $10,100.00" in stats
        assert "Day P&L -$100.00" in stats

        positions = app.query_one("#portfolio-positions", DataTable)
        assert positions.row_count == 1
        assert positions.get_row_at(0)[0] == "NVDA"

        closed = app.query_one("#portfolio-closed-trades", DataTable)
        assert closed.row_count == 1
        assert closed.get_row_at(0)[1] == "AAPL"

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert commands.closed == ["NVDA"]

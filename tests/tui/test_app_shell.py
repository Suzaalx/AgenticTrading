from __future__ import annotations

import pytest
from textual.widgets import Footer, TabbedContent, TabPane

from sentinel.tui.app import SentinelApp


@pytest.mark.asyncio
async def test_app_mounts_tabs_footer_and_keys() -> None:
    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one("#main-tabs", TabbedContent)
        assert [pane.id for pane in app.query(TabPane)] == [
            "dashboard",
            "run",
            "portfolio",
            "history",
            "backtest",
            "memory",
            "logs",
        ]
        assert app.query_one(Footer)

        for key, tab_id in {
            "f1": "dashboard",
            "f2": "run",
            "f3": "portfolio",
            "f4": "history",
            "f5": "backtest",
            "f6": "memory",
            "f7": "logs",
        }.items():
            await pilot.press(key)
            assert tabs.active == tab_id


@pytest.mark.asyncio
async def test_q_quits_without_active_run() -> None:
    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")

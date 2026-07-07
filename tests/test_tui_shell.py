from __future__ import annotations

import pytest
from textual.widgets import Footer, TabbedContent, TabPane

from sentinel.tui.app import SentinelApp


@pytest.mark.asyncio
async def test_tui_shell_mounts_with_tabs_and_footer() -> None:
    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one("#main-tabs", TabbedContent)
        assert len(app.query(TabPane)) == 7
        assert app.query_one(Footer)
        await pilot.press("f7")
        assert tabs.active == "logs"

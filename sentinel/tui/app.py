"""Textual application shell for Sentinel."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from sentinel.core.bus import EventBus
from sentinel.core.commands import CommandService, NullCommandService
from sentinel.core.events import EquityUpdated, Event, KillSwitchChanged, LogLine
from sentinel.tui.screens.backtest import BacktestScreen
from sentinel.tui.screens.dashboard import DashboardScreen
from sentinel.tui.screens.history import HistoryScreen
from sentinel.tui.screens.logs import LogsScreen
from sentinel.tui.screens.memory import MemoryScreen
from sentinel.tui.screens.portfolio import PortfolioScreen
from sentinel.tui.screens.run import RunMonitorScreen
from sentinel.tui.widgets.common import money, signed_money
from sentinel.tui.widgets.modals import (
    BacktestModal,
    ConfirmModal,
    HelpModal,
    NewRunModal,
    NewRunRequest,
)

TAB_IDS = {
    "f1": "dashboard",
    "f2": "run",
    "f3": "portfolio",
    "f4": "history",
    "f5": "backtest",
    "f6": "memory",
    "f7": "logs",
}


class SentinelApp(App[None]):
    """Sentinel terminal UI.

    Inject ``event_bus`` and ``command_service`` to wire the real orchestrator.
    """

    CSS_PATH = "styles.tcss"
    TITLE = "Sentinel"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("f1", "switch_tab('dashboard')", "F1 Dashboard"),
        Binding("f2", "switch_tab('run')", "F2 Run"),
        Binding("f3", "switch_tab('portfolio')", "F3 Portfolio"),
        Binding("f4", "switch_tab('history')", "F4 History"),
        Binding("f5", "switch_tab('backtest')", "F5 Backtest"),
        Binding("f6", "switch_tab('memory')", "F6 Memory"),
        Binding("f7", "switch_tab('logs')", "F7 Logs"),
        Binding("r", "new_run", "Run"),
        Binding("b", "new_backtest", "Backtest"),
        Binding("k", "toggle_kill", "Kill"),
        Binding("question_mark", "help", "Help"),
        Binding("d", "toggle_debate", "Debate"),
        Binding("x", "manual_close", "Close"),
        Binding("q", "request_quit", "Quit"),
        Binding("ctrl+c", "request_quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        command_service: CommandService | None = None,
    ) -> None:
        super().__init__()
        self.event_bus = event_bus or EventBus()
        self.command_service = command_service or NullCommandService()
        self.kill_switch_enabled = False
        self.total_equity = Decimal("0")
        self.day_pnl = Decimal("0")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="sentinel-status", classes="status-header")
        with TabbedContent(id="main-tabs"):
            yield TabPane("F1 Dashboard", DashboardScreen(), id="dashboard")
            yield TabPane("F2 Run", RunMonitorScreen(), id="run")
            yield TabPane("F3 Portfolio", PortfolioScreen(), id="portfolio")
            yield TabPane("F4 History", HistoryScreen(), id="history")
            yield TabPane("F5 Backtest", BacktestScreen(), id="backtest")
            yield TabPane("F6 Memory", MemoryScreen(), id="memory")
            yield TabPane("F7 Logs", LogsScreen(), id="logs")
        yield Footer()

    def on_mount(self) -> None:
        self._render_status_header()
        self.set_interval(30, self._render_status_header)
        starter = getattr(self.command_service, "start_background_services", None)
        if callable(starter):
            starter()
        self.run_worker(self._event_pump(), name="event-pump", exclusive=True)

    async def on_unmount(self) -> None:
        stopper = getattr(self.command_service, "stop_background_services", None)
        if callable(stopper):
            result = stopper()
            if inspect.isawaitable(result):
                await result

    async def _event_pump(self) -> None:
        async with self.event_bus.subscribe() as queue:
            while True:
                event = await queue.get()
                await self.dispatch_core_event(event)

    async def dispatch_core_event(self, event: Event) -> None:
        """Dispatch a core event to status chrome and every mounted tab."""

        if isinstance(event, EquityUpdated):
            self.total_equity = event.equity
            self.day_pnl = event.day_pnl
            self._render_status_header()
        elif isinstance(event, KillSwitchChanged):
            self.kill_switch_enabled = event.enabled
            self._render_status_header()
        elif isinstance(event, LogLine):
            self._render_status_header()

        for screen in self._tui_screens():
            handler = getattr(screen, "handle_event", None)
            if handler is None:
                continue
            result = handler(event)
            if inspect.isawaitable(result):
                await result

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to one of the F1-F7 tabs and focus its content."""

        self.query_one("#main-tabs", TabbedContent).active = tab_id
        self.call_after_refresh(self._focus_active_pane, tab_id)

    def _focus_active_pane(self, tab_id: str) -> None:
        try:
            pane = self.query_one(f"#{tab_id}", TabPane)
        except Exception:
            return
        for widget in pane.query("*"):
            if getattr(widget, "focusable", False):
                widget.focus()
                return

    def action_new_run(self) -> None:
        self.push_screen(NewRunModal(), self._handle_new_run)

    def _handle_new_run(self, request: object) -> None:
        if request is None:
            return
        self.run_worker(self._start_new_run(request), name="start-run", exclusive=False)

    async def _start_new_run(self, request: object) -> None:
        if not isinstance(request, NewRunRequest):
            return
        try:
            run_id = await self.command_service.start_run(
                request.symbol, request.run_date, request.depth
            )
        except NotImplementedError as exc:
            self.notify(str(exc), severity="error")
            return
        self.action_switch_tab("run")
        self.query_one(RunMonitorScreen).note_run_requested(run_id, request.symbol)

    def action_new_backtest(self) -> None:
        self.push_screen(BacktestModal(), self._handle_new_backtest)

    def _handle_new_backtest(self, config: object) -> None:
        if config is None:
            return
        if isinstance(config, dict):
            self.run_worker(self._start_new_backtest(config), name="start-backtest", exclusive=False)

    async def _start_new_backtest(self, config: dict[str, object]) -> None:
        try:
            await self.command_service.start_backtest(config)
        except NotImplementedError as exc:
            self.notify(str(exc), severity="error")
            return
        self.action_switch_tab("backtest")

    def action_toggle_kill(self) -> None:
        target = not self.kill_switch_enabled
        label = "ARM" if target else "DISARM"
        self.push_screen(
            ConfirmModal(f"{label} the filesystem kill switch?", confirm_label=label),
            lambda confirmed: self._handle_toggle_kill(target, bool(confirmed)),
        )

    def _handle_toggle_kill(self, target: bool, confirmed: bool) -> None:
        if not confirmed:
            return
        self.run_worker(self._toggle_kill(target), name="toggle-kill", exclusive=False)

    async def _toggle_kill(self, target: bool) -> None:
        try:
            await self.command_service.toggle_kill(target)
        except NotImplementedError as exc:
            self.notify(str(exc), severity="error")
            return
        self.kill_switch_enabled = target
        self._render_status_header()

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_toggle_debate(self) -> None:
        self.action_switch_tab("run")
        self.query_one(RunMonitorScreen).toggle_debate()

    def action_manual_close(self) -> None:
        self.action_switch_tab("portfolio")
        self.query_one(PortfolioScreen).action_manual_close()

    def action_request_quit(self) -> None:
        if self.query_one(RunMonitorScreen).has_active_run():
            self.push_screen(
                ConfirmModal("A run is active. Quit Sentinel?", confirm_label="Quit"),
                lambda confirmed: self.exit() if confirmed else None,
            )
        else:
            self.exit()

    def _render_status_header(self) -> None:
        now_et = datetime.now(UTC).astimezone(ZoneInfo("America/New_York"))
        market_open = (
            now_et.weekday() < 5
            and (now_et.hour, now_et.minute) >= (9, 30)
            and (now_et.hour, now_et.minute) < (16, 0)
        )
        market = f"{'●' if market_open else '○'} NYSE {'OPEN' if market_open else 'CLOSED'}"
        kill = "● ARMED" if self.kill_switch_enabled else "⛔ off"
        pnl_pct = (
            (self.day_pnl / self.total_equity) * Decimal("100") if self.total_equity else Decimal("0")
        )
        self.query_one("#sentinel-status", Static).update(
            "Sentinel • [PAPER] • "
            f"{market} {now_et:%H:%M} ET • "
            f"{kill} • "
            f"{money(self.total_equity)} {signed_money(self.day_pnl)} ({pnl_pct:+.2f}%)"
        )

    def _tui_screens(self) -> list[Any]:
        return [
            self.query_one(DashboardScreen),
            self.query_one(RunMonitorScreen),
            self.query_one(PortfolioScreen),
            self.query_one(HistoryScreen),
            self.query_one(BacktestScreen),
            self.query_one(MemoryScreen),
            self.query_one(LogsScreen),
        ]

"""Logs tab scaffold."""

from __future__ import annotations

import json
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Input, Select, Static

from sentinel.core.events import Event, LogLine
from sentinel.risk.audit import read_audit
from sentinel.tui.widgets.common import compact_time


class LogsScreen(Widget):
    """Audit ledger and app log tail with level filtering and search."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("/", "focus_search", "Search logs"),
        Binding("space", "toggle_pause", "Pause logs", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__(id="logs-screen")
        self._app_lines: list[tuple[str, str]] = []
        self._audit_lines: list[str] = []
        self._paused = False
        self._search = ""
        self._level = "all"

    def compose(self) -> ComposeResult:
        with Vertical(classes="screen-body"):
            yield Select(
                [("All levels", "all"), ("Info+", "info"), ("Warning+", "warning"), ("Errors", "error")],
                value="all",
                id="logs-filter",
            )
            yield Input(placeholder="Search log text", id="logs-search")
            yield Static(id="logs-tail", classes="panel")

    def on_mount(self) -> None:
        self.hydrate()

    def hydrate(self) -> None:
        self._audit_lines = []
        for record in read_audit(limit=100):
            payload = json.dumps(record["payload"], default=str, sort_keys=True)
            self._audit_lines.append(
                f"{record['ts'][:19]} AUDIT {record['kind']} actor={record['actor']} {payload}"
            )
        self._render()

    def handle_event(self, event: Event) -> None:
        if isinstance(event, LogLine):
            context = f" {event.context}" if event.context else ""
            self._app_lines.append(
                (event.level, f"{compact_time(event.ts)} {event.level.upper()} {event.message}{context}")
            )
            if len(self._app_lines) > 200:
                del self._app_lines[: len(self._app_lines) - 200]
            if not self._paused:
                self._render()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "logs-filter":
            self._level = str(event.value)
            self._render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "logs-search":
            self._search = event.value.strip().lower()
            self._render()

    def on_mouse_scroll_up(self) -> None:
        self._paused = True
        self._render()

    def action_focus_search(self) -> None:
        self.query_one("#logs-search", Input).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Let the search box receive spaces; pause toggle applies elsewhere."""

        return not (action == "toggle_pause" and isinstance(self.app.focused, Input))

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        self._render()

    def _render(self) -> None:
        threshold = {"all": 0, "debug": 0, "info": 1, "warning": 2, "error": 3}
        level_rank = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
        min_rank = threshold.get(self._level, 0)
        app_lines = [
            line
            for level, line in self._app_lines
            if level_rank.get(level, 0) >= min_rank and self._matches(line)
        ]
        audit_lines = [line for line in self._audit_lines if self._matches(line)]
        header = f"Logs {'[PAUSED]' if self._paused else '[FOLLOW]'} filter={self._level}"
        lines = [header, "— Audit ledger —", *audit_lines[-60:], "— App log —", *app_lines[-80:]]
        self.query_one("#logs-tail", Static).update("\n".join(lines))

    def _matches(self, line: str) -> bool:
        return not self._search or self._search in line.lower()

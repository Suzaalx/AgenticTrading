"""Modal dialogs used by the Sentinel TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from sentinel.core.commands import Depth


@dataclass(frozen=True)
class NewRunRequest:
    """Validated inputs from the new-run dialog."""

    symbol: str
    run_date: date | None
    depth: Depth


class ConfirmModal(ModalScreen[bool]):
    """Simple yes/no confirmation modal."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, message: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Container(id="confirm-modal"):
            yield Static(self.message, id="confirm-message")
            with Horizontal(classes="modal-buttons"):
                yield Button(self.confirm_label, id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class HelpModal(ModalScreen[None]):
    """Keyboard help overlay."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("?", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="help-modal"):
            yield Static(
                "Sentinel keys\n\n"
                "F1-F7 switch screens\n"
                "R new decision run\n"
                "B new backtest\n"
                "K toggle kill switch\n"
                "D debate view in Run Monitor\n"
                "X close selected portfolio position\n"
                "Q quit",
                id="help-text",
            )

    def action_dismiss(self) -> None:
        self.dismiss(None)


class NewRunModal(ModalScreen[NewRunRequest | None]):
    """New decision-run form."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Start"),
    ]

    def compose(self) -> ComposeResult:
        today = date.today().isoformat()
        with Container(id="new-run-modal"):
            yield Label("New decision run", classes="modal-title")
            with Vertical(classes="form-fields"):
                yield Label("Symbol")
                yield Input(value="NVDA", placeholder="NVDA", id="new-run-symbol")
                yield Label("Date")
                yield Input(value=today, placeholder=today, id="new-run-date")
                yield Label("Depth")
                yield Select(
                    [("Fast", "fast"), ("Standard", "standard"), ("Deep", "deep")],
                    value="standard",
                    id="new-run-depth",
                )
                yield Static("Estimated LLM cost: fast ~$0.10 • standard ~$0.30 • deep ~$0.50")
            with Horizontal(classes="modal-buttons"):
                yield Button("Start run", id="new-run-start", variant="success")
                yield Button("Cancel", id="new-run-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        symbol = self.query_one("#new-run-symbol", Input).value.strip().upper()
        raw_date = self.query_one("#new-run-date", Input).value.strip()
        depth_value = str(self.query_one("#new-run-depth", Select).value)
        if depth_value not in {"fast", "standard", "deep"}:
            depth: Depth = "standard"
        else:
            depth = cast(Depth, depth_value)
        if not symbol:
            self.query_one("#new-run-symbol", Input).value = "NVDA"
            symbol = "NVDA"
        try:
            run_date = date.fromisoformat(raw_date) if raw_date else None
        except ValueError:
            run_date = date.today()
        self.dismiss(NewRunRequest(symbol=symbol, run_date=run_date, depth=depth))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-run-start":
            self.action_submit()
        else:
            self.action_cancel()


class BacktestModal(ModalScreen[dict[str, object] | None]):
    """Backtest launcher with agent-mode cost confirmation."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Start"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="backtest-modal"):
            yield Label("Backtest", classes="modal-title")
            yield Input(value="NVDA", placeholder="Symbols", id="backtest-symbols")
            yield Select([("Rule", "rule"), ("Agent", "agent")], value="rule", id="backtest-mode")
            yield Input(value="sma_cross", placeholder="Rule strategy or agent depth", id="backtest-depth")
            yield Input(value="", placeholder='Type "run" for agent mode', id="backtest-confirm")
            yield Static(
                "Estimated LLM cost: agent mode ~$0.30 per cadence step; rule mode is free."
            )
            with Horizontal(classes="modal-buttons"):
                yield Button("Start backtest", id="backtest-start", variant="success")
                yield Button("Cancel", id="backtest-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        symbols = self.query_one("#backtest-symbols", Input).value.strip()
        mode = str(self.query_one("#backtest-mode", Select).value)
        depth = self.query_one("#backtest-depth", Input).value.strip() or "standard"
        confirm = self.query_one("#backtest-confirm", Input).value.strip().lower()
        if mode == "agent" and confirm != "run":
            self.query_one("#backtest-confirm", Input).value = "type run to confirm agent cost"
            return
        self.dismiss({"symbol": symbols or "NVDA", "symbols": symbols or "NVDA", "depth": depth, "mode": mode})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "backtest-start":
            self.action_submit()
        else:
            self.action_cancel()

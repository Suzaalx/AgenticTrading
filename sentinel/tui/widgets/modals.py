"""Modal dialogs used by the Sentinel TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from sentinel.core.commands import Depth
from sentinel.store.db import sentinel_home
from sentinel.tui.widgets.common import decimal_from, fetch_rows


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
                "C option chain modal\n"
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


class ChainModal(ModalScreen[None]):
    """Near-the-money option chain modal with provenance labels."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("c", "dismiss", "Close"),
    ]

    def __init__(self, run_id: str | None = None) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        with Container(id="chain-modal"):
            yield Label("Option chain", classes="modal-title")
            yield Static(id="chain-header")
            yield DataTable(id="chain-table")
            yield Static("Provenance is shown above: pricing_source live_chain vs synthetic_bsm.")

    def on_mount(self) -> None:
        table = self.query_one("#chain-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("EXP", "K", "TYPE", "BID", "ASK", "MID", "Δ", "Γ", "VEGA", "THETA", "SRC")
        self._hydrate()

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def _hydrate(self) -> None:
        run_id = self.run_id or _latest_chain_run_id()
        header = self.query_one("#chain-header", Static)
        table = self.query_one("#chain-table", DataTable)
        table.clear()
        if run_id is None:
            header.update("No option chain artifact found.")
            return
        meta = _chain_meta(run_id)
        rows = _chain_rows(run_id)
        provenance = str(meta.get("pricing_source") or _row_value(rows, "source") or "unknown")
        iv_rank = _format_optional_float(meta.get("iv_rank"), scale=100)
        spot = meta.get("spot") or _row_value(rows, "spot") or "—"
        header.update(f"Run {run_id} • spot {spot} • IV rank {iv_rank} • provenance {provenance}")
        if not rows:
            table.add_row("—", "—", "—", "—", "—", "—", "—", "—", "—", "—", provenance)
            return
        for row in _near_money(rows):
            bid = decimal_from(row.get("bid"))
            ask = decimal_from(row.get("ask"))
            mid = (bid + ask) / Decimal("2")
            table.add_row(
                str(row.get("expiry") or "")[:10],
                _fmt_decimal(row.get("strike")),
                str(row.get("kind") or "").upper(),
                _fmt_decimal(bid),
                _fmt_decimal(ask),
                _fmt_decimal(mid),
                _fmt_float(row.get("delta")),
                _fmt_float(row.get("gamma"), precision=4),
                _fmt_float(row.get("vega")),
                _fmt_float(row.get("theta")),
                str(row.get("source") or provenance),
            )


def _latest_chain_run_id() -> str | None:
    rows = fetch_rows(
        "SELECT run_id FROM option_chain_meta ORDER BY as_of DESC LIMIT 1"
    )
    if rows:
        return str(rows[0]["run_id"])
    runs_dir = sentinel_home() / "runs"
    if not runs_dir.exists():
        return None
    candidates = sorted(runs_dir.glob("*/chain.parquet"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0].parent.name if candidates else None


def _chain_meta(run_id: str) -> dict[str, object]:
    rows = fetch_rows("SELECT * FROM option_chain_meta WHERE run_id = ?", (run_id,))
    return dict(rows[0]) if rows else {}


def _chain_rows(run_id: str) -> list[dict[str, object]]:
    path = sentinel_home() / "runs" / run_id / "chain.parquet"
    if not path.exists():
        return []
    try:
        import pandas as pd
    except ImportError:
        return []
    try:
        frame = pd.read_parquet(Path(path))
    except (OSError, ValueError, ImportError):
        return []
    return [cast(dict[str, object], row) for row in frame.to_dict(orient="records")]


def _near_money(rows: list[dict[str, object]], *, limit: int = 12) -> list[dict[str, object]]:
    spot = decimal_from(_row_value(rows, "spot"))
    ordered = sorted(rows, key=lambda row: (abs(decimal_from(row.get("strike")) - spot), str(row.get("expiry")), str(row.get("kind"))))
    return ordered[:limit]


def _row_value(rows: list[dict[str, object]], key: str) -> object | None:
    for row in rows:
        value = row.get(key)
        if value not in {None, ""}:
            return value
    return None


def _format_optional_float(value: object, *, scale: float = 1.0) -> str:
    try:
        number = float(str(value)) * scale
    except (TypeError, ValueError):
        return "—"
    return f"{number:.1f}%"


def _fmt_float(value: object, *, precision: int = 2) -> str:
    try:
        return f"{float(str(value)):.{precision}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_decimal(value: object) -> str:
    return f"{decimal_from(value):,.2f}"


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

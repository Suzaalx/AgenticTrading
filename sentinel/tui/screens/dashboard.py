"""Dashboard tab for portfolio overview and quick actions."""

from __future__ import annotations

from decimal import Decimal

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from sentinel.core.events import CostIncurred, EquityUpdated, Event, OrderSubmitted, QuoteTick
from sentinel.tui.widgets.common import (
    EquityPlot,
    decimal_from,
    fetch_rows,
    latest_equity_row,
    money,
    row_value,
    signed_money,
)


class DashboardScreen(Widget):
    """Live dashboard hydrated from the store and updated from events."""

    def __init__(self) -> None:
        super().__init__(id="dashboard-screen")
        self._positions: list[dict[str, object]] = []
        self._marks: dict[str, Decimal] = {}
        self._equity_values: list[float] = []
        self._cash = Decimal("0")
        self._equity = Decimal("0")
        self._day_pnl = Decimal("0")
        self._today_orders = 0
        self._today_cost = Decimal("0")

    def compose(self) -> ComposeResult:
        with Vertical(classes="screen-body"):
            with Horizontal(id="dashboard-top"):
                yield Static(id="dashboard-summary", classes="panel")
                yield EquityPlot(id="dashboard-equity-plot", classes="panel")
            with Horizontal(id="dashboard-middle"):
                yield Static(
                    "[R] Run decision…\n[B] Backtest…\n[K] Kill switch",
                    id="dashboard-actions",
                    classes="panel",
                )
                yield DataTable(id="dashboard-positions", classes="panel")
            with Horizontal(id="dashboard-bottom"):
                yield Static(id="dashboard-decisions", classes="panel")
                yield Static(id="dashboard-today", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#dashboard-positions", DataTable)
        table.cursor_type = "row"
        table.add_columns("SYM", "QTY", "AVG", "LAST", "P&L", "STOP", "RUN")
        self.hydrate()

    def hydrate(self) -> None:
        """Hydrate dashboard state once from SQLite."""

        self._load_portfolio()
        self._load_equity_curve()
        self._load_today()
        self._render_summary()
        self._render_positions()
        self._render_decisions()
        self._render_today()
        self.query_one("#dashboard-equity-plot", EquityPlot).update_series(
            self._equity_values, title="Equity curve (30d)"
        )

    def handle_event(self, event: Event) -> None:
        """Apply live events without polling the store."""

        if isinstance(event, EquityUpdated):
            self._equity = event.equity
            self._cash = event.cash
            self._day_pnl = event.day_pnl
            self._equity_values.append(float(event.equity))
            self._render_summary()
            self._render_today()
            self.query_one("#dashboard-equity-plot", EquityPlot).update_series(
                self._equity_values[-30:], title="Equity curve (30d)"
            )
        elif isinstance(event, QuoteTick):
            self._marks[event.quote.symbol] = event.quote.price
            self._render_positions()
        elif isinstance(event, CostIncurred):
            self._today_cost += event.cost_usd
            self._render_today()
        elif isinstance(event, OrderSubmitted):
            self._today_orders += 1
            self._render_today()

    def _load_portfolio(self) -> None:
        rows = fetch_rows(
            "SELECT symbol, qty, avg_cost, stop_pct, tp_pct, opened_at, horizon_days, "
            "source_run_id FROM positions ORDER BY symbol"
        )
        self._positions = [dict(row) for row in rows]
        latest = latest_equity_row()
        if latest is not None:
            self._cash = decimal_from(latest["cash"])
            self._equity = decimal_from(latest["equity"])
            self._day_pnl = decimal_from(latest["day_pnl"])
        else:
            basis = sum(
                decimal_from(row_value(row, "qty")) * decimal_from(row_value(row, "avg_cost"))
                for row in rows
            )
            self._cash = Decimal("0")
            self._equity = basis
            self._day_pnl = Decimal("0")

    def _load_equity_curve(self) -> None:
        rows = fetch_rows("SELECT equity FROM equity_curve ORDER BY ts DESC LIMIT 30")
        self._equity_values = [float(decimal_from(row["equity"])) for row in reversed(rows)]

    def _load_today(self) -> None:
        order_rows = fetch_rows("SELECT COUNT(*) AS count FROM orders WHERE date(created_at)=date('now')")
        cost_rows = fetch_rows("SELECT SUM(cost_usd) AS cost FROM costs WHERE date(ts)=date('now')")
        self._today_orders = int(order_rows[0]["count"]) if order_rows else 0
        self._today_cost = decimal_from(cost_rows[0]["cost"] if cost_rows else None)

    def _render_summary(self) -> None:
        exposure = Decimal("0")
        if self._equity:
            position_value = sum(
                (
                    decimal_from(position.get("qty"))
                    * decimal_from(position.get("avg_cost"))
                    for position in self._positions
                ),
                Decimal("0"),
            )
            exposure = (position_value / self._equity) * Decimal("100")
        day_pct = (self._day_pnl / self._equity * Decimal("100")) if self._equity else Decimal("0")
        self.query_one("#dashboard-summary", Static).update(
            "Portfolio\n"
            f"Cash        {money(self._cash)}\n"
            f"Equity      {money(self._equity)}\n"
            f"Day P&L     {signed_money(self._day_pnl)} ({day_pct:+.2f}%)\n"
            f"Open pos    {len(self._positions)}\n"
            f"Exposure    {exposure:.0f}% / 80%"
        )

    def _render_positions(self) -> None:
        table = self.query_one("#dashboard-positions", DataTable)
        table.clear()
        for position in self._positions:
            symbol = str(position.get("symbol", ""))
            qty = decimal_from(position.get("qty"))
            avg = decimal_from(position.get("avg_cost"))
            last = self._marks.get(symbol, avg)
            pnl = (last - avg) * qty
            stop = position.get("stop_pct")
            stop_text = f"{decimal_from(stop):+.0f}%" if stop is not None else "—"
            run_id = str(position.get("source_run_id") or "—")
            table.add_row(
                symbol,
                f"{qty:g}",
                f"{avg:,.2f}",
                f"{last:,.2f}",
                signed_money(pnl),
                stop_text,
                run_id[:6],
                key=symbol,
            )

    def _render_decisions(self) -> None:
        rows = fetch_rows(
            "SELECT as_of, symbol, action, verdict, cost_usd FROM runs "
            "ORDER BY COALESCE(finished_at, created_at, as_of) DESC LIMIT 5"
        )
        if not rows:
            text = "Recent decisions\nNo completed decisions yet."
        else:
            lines = ["Recent decisions"]
            for row in rows:
                as_of = str(row["as_of"] or "")[:10]
                lines.append(
                    f"{as_of} {row['symbol'] or '—'} {row['action'] or '—'} "
                    f"{row['verdict'] or '—'} {money(row['cost_usd'])}"
                )
            text = "\n".join(lines)
        self.query_one("#dashboard-decisions", Static).update(text)

    def _render_today(self) -> None:
        day_pct = (self._day_pnl / self._equity * Decimal("100")) if self._equity else Decimal("0")
        self.query_one("#dashboard-today", Static).update(
            "Today\n"
            f"Orders {self._today_orders}/10\n"
            f"LLM spend {money(self._today_cost)}\n"
            f"Loss cap -3% (now {day_pct:+.2f}%)\n"
            "Sched ✓"
        )

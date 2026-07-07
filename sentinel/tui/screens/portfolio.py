"""Portfolio tab with live positions, trades, allocation, and stats."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from sentinel.core.commands import CommandService
from sentinel.core.events import EquityUpdated, Event, OrderFilled, QuoteTick
from sentinel.store.db import sentinel_home
from sentinel.tui.widgets.common import (
    EquityPlot,
    decimal_from,
    fetch_rows,
    latest_equity_row,
    money,
    signed_money,
    signed_percent,
)
from sentinel.tui.widgets.modals import ConfirmModal


class PortfolioScreen(Widget):
    """Portfolio screen hydrated from store and updated from portfolio events."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("x", "manual_close", "Close position")]

    def __init__(self) -> None:
        super().__init__(id="portfolio-screen")
        self._positions: list[dict[str, object]] = []
        self._option_positions: list[dict[str, object]] = []
        self._position_symbols: list[str] = []
        self._marks: dict[str, Decimal] = {}
        self._equity_values: list[float] = []
        self._cash = Decimal("0")
        self._equity = Decimal("0")
        self._day_pnl = Decimal("0")

    def compose(self) -> ComposeResult:
        with Vertical(classes="screen-body"):
            yield Static(id="portfolio-stats", classes="panel")
            with Horizontal(id="portfolio-tables"):
                yield DataTable(id="portfolio-positions", classes="panel")
                yield DataTable(id="portfolio-closed-trades", classes="panel")
            yield DataTable(id="portfolio-option-positions", classes="panel option-panel")
            yield Static(id="portfolio-allocation", classes="panel")
            with Horizontal(id="portfolio-charts"):
                yield EquityPlot(id="portfolio-equity-plot", classes="panel")
                yield EquityPlot(id="portfolio-drawdown-plot", classes="panel")

    def on_mount(self) -> None:
        positions = self.query_one("#portfolio-positions", DataTable)
        positions.cursor_type = "row"
        positions.add_columns("SYM", "QTY", "AVG", "LAST", "P&L", "ALLOC", "RUN")
        options = self.query_one("#portfolio-option-positions", DataTable)
        options.cursor_type = "row"
        options.add_columns(
            "STRATEGY", "LEGS", "DTE", "NET PREM", "MARK", "UNREAL P&L", "MAX LOSS", "COLLATERAL", "VENUE"
        )
        trades = self.query_one("#portfolio-closed-trades", DataTable)
        trades.cursor_type = "row"
        trades.add_columns("ORDER", "SYM", "SIDE", "QTY", "PRICE", "P&L", "TS")
        self.hydrate()

    def hydrate(self) -> None:
        self._load_positions()
        self._load_option_positions()
        self._load_equity()
        self._render_positions()
        self._render_option_positions()
        self._render_closed_trades()
        self._render_stats()
        self._render_allocation()
        self._render_plots()

    def handle_event(self, event: Event) -> None:
        if isinstance(event, EquityUpdated):
            self._cash = event.cash
            self._equity = event.equity
            self._day_pnl = event.day_pnl
            self._equity_values.append(float(event.equity))
            self._render_stats()
            self._render_allocation()
            self._render_plots()
        elif isinstance(event, QuoteTick):
            self._marks[event.quote.symbol] = event.quote.price
            self._render_positions()
            self._render_option_positions()
            self._render_allocation()
        elif isinstance(event, OrderFilled):
            self._append_trade(event.order_id, event.fill.qty, event.fill.price)

    def action_manual_close(self) -> None:
        symbol = self._selected_symbol()
        if symbol is None:
            self.notify("No position selected", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(f"Close {symbol} manually?", confirm_label="Close"),
            lambda confirmed: self._after_close_confirm(symbol, bool(confirmed)),
        )

    def _after_close_confirm(self, symbol: str, confirmed: bool) -> None:
        if not confirmed:
            return
        self.run_worker(self._close_symbol(symbol), name=f"close-{symbol}", exclusive=True)

    async def _close_symbol(self, symbol: str) -> None:
        app = cast(Any, self.app)
        command_service = cast(CommandService, app.command_service)
        try:
            await command_service.close_position(symbol)
        except NotImplementedError as exc:
            self.notify(str(exc), severity="error")

    def _load_positions(self) -> None:
        rows = fetch_rows(
            "SELECT symbol, qty, avg_cost, stop_pct, tp_pct, opened_at, horizon_days, "
            "source_run_id FROM positions ORDER BY symbol"
        )
        self._positions = [dict(row) for row in rows]
        self._position_symbols = [str(row["symbol"]) for row in rows]

    def _load_option_positions(self) -> None:
        rows = fetch_rows(
            "SELECT position_id, underlying, strategy, legs_json, open_premium, max_loss, collateral, "
            "opened_at, expiry, source_run_id, venue FROM option_positions ORDER BY venue DESC, underlying"
        )
        self._option_positions = [dict(row) for row in rows]

    def _load_equity(self) -> None:
        latest = latest_equity_row()
        if latest is not None:
            self._cash = decimal_from(latest["cash"])
            self._equity = decimal_from(latest["equity"])
            self._day_pnl = decimal_from(latest["day_pnl"])
        rows = fetch_rows("SELECT equity FROM equity_curve ORDER BY ts")
        self._equity_values = [float(decimal_from(row["equity"])) for row in rows]
        if latest is None and self._positions:
            self._equity = sum(
                decimal_from(position.get("qty")) * decimal_from(position.get("avg_cost"))
                for position in self._positions
            )

    def _render_option_positions(self) -> None:
        table = self.query_one("#portfolio-option-positions", DataTable)
        table.clear()
        if not self._option_positions:
            table.add_row("Options", "No open option positions.", "—", "—", "—", "—", "—", "—", "PAPER")
            return
        greeks = {"delta": Decimal("0"), "gamma": Decimal("0"), "vega": Decimal("0"), "theta": Decimal("0")}
        for section in ("robinhood_agentic", "paper"):
            section_positions = [p for p in self._option_positions if str(p.get("venue") or "paper") == section]
            if not section_positions:
                continue
            label = "LIVE OPTIONS" if section != "paper" else "PAPER OPTIONS"
            badge = _venue_badge(section)
            table.add_row(label, "────────", "—", "—", "—", "—", "—", "—", badge)
            for position in section_positions:
                legs = _parse_legs(position.get("legs_json"))
                leg_text = _compact_legs(legs)
                dte = _dte(position.get("expiry"))
                mark = _option_mark(legs)
                open_premium = decimal_from(position.get("open_premium"))
                pnl = mark - open_premium
                run_id = str(position.get("source_run_id") or "")
                position_greeks = _position_greeks(legs, run_id)
                for key, value in position_greeks.items():
                    greeks[key] += value
                table.add_row(
                    str(position.get("strategy") or "—"),
                    leg_text,
                    str(dte),
                    money(open_premium),
                    money(mark),
                    signed_money(pnl),
                    money(decimal_from(position.get("max_loss"))),
                    money(decimal_from(position.get("collateral"))),
                    _venue_badge(str(position.get("venue") or "paper")),
                    key=str(position.get("position_id") or f"{run_id}-{leg_text}"),
                )
        table.add_row(
            "NET GREEKS",
            f"Δ {greeks['delta']:+.2f}  Γ {greeks['gamma']:+.2f}  V {greeks['vega']:+.2f}  Θ {greeks['theta']:+.2f}",
            "—", "—", "—", "—", "—", "—", "PORTFOLIO",
        )

    def _render_positions(self) -> None:
        table = self.query_one("#portfolio-positions", DataTable)
        table.clear()
        self._position_symbols.clear()
        for position in self._positions:
            symbol = str(position.get("symbol", ""))
            self._position_symbols.append(symbol)
            qty = decimal_from(position.get("qty"))
            avg = decimal_from(position.get("avg_cost"))
            last = self._marks.get(symbol, avg)
            market_value = qty * last
            pnl = (last - avg) * qty
            allocation = (market_value / self._equity * Decimal("100")) if self._equity else Decimal("0")
            table.add_row(
                symbol,
                f"{qty:g}",
                f"{avg:,.2f}",
                f"{last:,.2f}",
                signed_money(pnl),
                f"{allocation:.1f}%",
                str(position.get("source_run_id") or "—")[:8],
                key=symbol,
            )

    def _render_closed_trades(self) -> None:
        pnl_by_order = _closed_trade_pnl_by_order()
        rows = fetch_rows(
            "SELECT o.order_id, o.symbol, o.side, f.qty, f.price, f.ts "
            "FROM orders o JOIN fills f ON o.order_id=f.order_id "
            "WHERE o.status IN ('filled','closed') OR o.reason IN ('manual','time_exit','stop_loss','take_profit') "
            "ORDER BY f.ts DESC LIMIT 20"
        )
        table = self.query_one("#portfolio-closed-trades", DataTable)
        table.clear()
        for row in rows:
            table.add_row(
                str(row["order_id"])[:8],
                row["symbol"] or "—",
                row["side"] or "—",
                str(row["qty"] or "—"),
                money(row["price"]),
                signed_money(pnl_by_order.get(str(row["order_id"]), Decimal("0"))),
                str(row["ts"] or "")[:16],
            )

    def _append_trade(self, order_id: str, qty: Decimal, price: Decimal) -> None:
        table = self.query_one("#portfolio-closed-trades", DataTable)
        pnl = _closed_trade_pnl_by_order().get(order_id, Decimal("0"))
        table.add_row(order_id[:8], "—", "—", f"{qty:g}", money(price), signed_money(pnl), "live")

    def _render_stats(self) -> None:
        total_return = Decimal("0")
        max_dd = Decimal("0")
        if len(self._equity_values) >= 2 and self._equity_values[0]:
            total_return = (
                Decimal(str(self._equity_values[-1])) / Decimal(str(self._equity_values[0])) - 1
            ) * Decimal("100")
            peak = self._equity_values[0]
            drawdowns: list[float] = []
            for value in self._equity_values:
                peak = max(peak, value)
                drawdowns.append((value / peak - 1) * 100 if peak else 0)
            max_dd = Decimal(str(min(drawdowns)))
        self.query_one("#portfolio-stats", Static).update(
            "Stats  "
            f"Equity {money(self._equity)}  Cash {money(self._cash)}  "
            f"Day P&L {signed_money(self._day_pnl)}  "
            f"Total return {signed_percent(total_return)}  "
            "Sharpe-to-date 0.00  "
            f"Max DD {signed_percent(max_dd)}  "
            "Win rate 0.0%"
        )

    def _render_allocation(self) -> None:
        lines = ["Allocation vs mandate cap"]
        for position in self._positions:
            symbol = str(position.get("symbol", ""))
            qty = decimal_from(position.get("qty"))
            avg = decimal_from(position.get("avg_cost"))
            last = self._marks.get(symbol, avg)
            pct = (qty * last / self._equity * Decimal("100")) if self._equity else Decimal("0")
            filled = min(20, max(0, int(pct / Decimal("4"))))
            bar = "█" * filled + "░" * (20 - filled)
            lines.append(f"{symbol:<8} {bar} {pct:.1f}% / 80%")
        if len(lines) == 1:
            lines.append("No open positions.")
        if self._option_positions:
            live_count = sum(1 for position in self._option_positions if position.get("venue") != "paper")
            paper_count = len(self._option_positions) - live_count
            lines.append(f"Options sections: LIVE {live_count} / PAPER {paper_count}")
        self.query_one("#portfolio-allocation", Static).update("\n".join(lines))

    def _render_plots(self) -> None:
        equity_plot = self.query_one("#portfolio-equity-plot", EquityPlot)
        equity_plot.update_series(self._equity_values, title="Equity curve")
        drawdowns = self._drawdowns(self._equity_values)
        self.query_one("#portfolio-drawdown-plot", EquityPlot).update_series(
            drawdowns, title="Drawdown"
        )

    def _selected_symbol(self) -> str | None:
        if not self._position_symbols:
            return None
        table = self.query_one("#portfolio-positions", DataTable)
        row_index = max(0, min(table.cursor_row, len(self._position_symbols) - 1))
        return self._position_symbols[row_index]

    @staticmethod
    def _drawdowns(values: list[float]) -> list[float]:
        peak = values[0] if values else 0
        result: list[float] = []
        for value in values:
            peak = max(peak, value)
            result.append((value / peak - 1) * 100 if peak else 0)
        return result


def _closed_trade_pnl_by_order() -> dict[str, Decimal]:
    rows = fetch_rows(
        "SELECT o.order_id, o.symbol, o.side, f.qty, f.price, f.commission_usd, f.ts "
        "FROM orders o JOIN fills f ON o.order_id=f.order_id "
        "WHERE o.status IN ('filled','closed') OR o.reason IN ('manual','time_exit','stop_loss','take_profit') "
        "ORDER BY f.ts ASC"
    )
    positions: dict[str, tuple[Decimal, Decimal]] = {}
    pnl_by_order: dict[str, Decimal] = {}
    for row in rows:
        symbol = str(row["symbol"] or "")
        side = str(row["side"] or "")
        qty = decimal_from(row["qty"])
        price = decimal_from(row["price"])
        commission = decimal_from(row["commission_usd"])
        held_qty, held_cost = positions.get(symbol, (Decimal("0"), Decimal("0")))
        if side == "buy":
            positions[symbol] = (held_qty + qty, held_cost + (price * qty) + commission)
            continue
        if side != "sell" or qty <= 0 or held_qty <= 0:
            continue
        closing_qty = min(qty, held_qty)
        cost_basis = held_cost / held_qty * closing_qty
        pnl_by_order[str(row["order_id"])] = (price * closing_qty) - commission - cost_basis
        remaining_qty = held_qty - closing_qty
        remaining_cost = held_cost - cost_basis
        positions[symbol] = (remaining_qty, remaining_cost) if remaining_qty > 0 else (
            Decimal("0"),
            Decimal("0"),
        )
    return pnl_by_order


def _parse_legs(raw: object) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return [cast(dict[str, Any], item) for item in decoded if isinstance(item, dict)]


def _compact_legs(legs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for leg in legs:
        contract = cast(dict[str, Any], leg.get("contract") or {})
        side = "B" if leg.get("side") == "buy" else "S"
        kind = str(contract.get("kind") or "?")[:1].upper()
        strike = decimal_from(contract.get("strike"))
        expiry = str(contract.get("expiry") or "")[5:10]
        parts.append(f"{side}{leg.get('contracts', 1)} {strike:g}{kind} {expiry}")
    return " / ".join(parts) if parts else "—"


def _dte(raw_expiry: object) -> int:
    try:
        expiry = date.fromisoformat(str(raw_expiry)[:10])
    except ValueError:
        return 0
    return max(0, (expiry - datetime.now(UTC).date()).days)


def _option_mark(legs: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for leg in legs:
        contract = cast(dict[str, Any], leg.get("contract") or {})
        multiplier = decimal_from(contract.get("multiplier"), Decimal("100"))
        contracts = decimal_from(leg.get("contracts"), Decimal("1"))
        price = decimal_from(leg.get("limit_price"))
        sign = Decimal("1") if leg.get("side") == "buy" else Decimal("-1")
        total += sign * price * contracts * multiplier
    return total


def _venue_badge(venue: str) -> str:
    return "LIVE" if venue != "paper" else "PAPER"


def _position_greeks(legs: list[dict[str, Any]], run_id: str) -> dict[str, Decimal]:
    quote_greeks = _chain_greeks(run_id)
    totals = {"delta": Decimal("0"), "gamma": Decimal("0"), "vega": Decimal("0"), "theta": Decimal("0")}
    for leg in legs:
        contract = cast(dict[str, Any], leg.get("contract") or {})
        symbol = str(contract.get("contract_symbol") or "")
        values = quote_greeks.get(symbol, {})
        sign = Decimal("1") if leg.get("side") == "buy" else Decimal("-1")
        contracts = decimal_from(leg.get("contracts"), Decimal("1"))
        multiplier = decimal_from(contract.get("multiplier"), Decimal("100"))
        scale = sign * contracts * multiplier
        for key in totals:
            totals[key] += decimal_from(values.get(key)) * scale
    if not any(totals.values()):
        candidate = _candidate_greeks(run_id, legs)
        totals.update(candidate)
    return totals


def _chain_greeks(run_id: str) -> dict[str, dict[str, object]]:
    if not run_id:
        return {}
    path = sentinel_home() / "runs" / run_id / "chain.parquet"
    if not path.exists():
        return {}
    try:
        import pandas as pd
        frame = pd.read_parquet(Path(path), columns=["contract_symbol", "delta", "gamma", "vega", "theta"])
    except (ImportError, OSError, ValueError):
        return {}
    result: dict[str, dict[str, object]] = {}
    for row in frame.to_dict(orient="records"):
        typed = cast(dict[str, object], row)
        result[str(typed.get("contract_symbol") or "")] = typed
    return result


def _candidate_greeks(run_id: str, legs: list[dict[str, Any]]) -> dict[str, Decimal]:
    if not run_id:
        return {"delta": Decimal("0"), "gamma": Decimal("0"), "vega": Decimal("0"), "theta": Decimal("0")}
    path = sentinel_home() / "runs" / run_id / "candidates.json"
    if not path.exists():
        return {"delta": Decimal("0"), "gamma": Decimal("0"), "vega": Decimal("0"), "theta": Decimal("0")}
    try:
        candidates = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"delta": Decimal("0"), "gamma": Decimal("0"), "vega": Decimal("0"), "theta": Decimal("0")}
    symbols = sorted(str(cast(dict[str, Any], leg.get("contract") or {}).get("contract_symbol") or "") for leg in legs)
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        candidate_legs = candidate.get("legs")
        if not isinstance(candidate_legs, list):
            continue
        candidate_symbols = sorted(
            str(cast(dict[str, Any], cast(dict[str, Any], item).get("contract") or {}).get("contract_symbol") or "")
            for item in candidate_legs
            if isinstance(item, dict)
        )
        if candidate_symbols == symbols:
            return {
                "delta": decimal_from(candidate.get("net_delta")),
                "gamma": Decimal("0"),
                "vega": decimal_from(candidate.get("net_vega")),
                "theta": decimal_from(candidate.get("net_theta")),
            }
    return {"delta": Decimal("0"), "gamma": Decimal("0"), "vega": Decimal("0"), "theta": Decimal("0")}

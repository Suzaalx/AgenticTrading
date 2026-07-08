"""F8 Live RH — real-time Robinhood account view via the Agentic Trading MCP."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from sentinel.execution.rh_viewer import (
    build_session,
    dec,
    fetch_accounts,
    fetch_equity_positions,
    fetch_option_positions,
    fetch_portfolio,
    mask,
)

_POLL_SECONDS = 30
_NO_TOKEN_MSG = (
    "RH_AGENTIC_MCP_TOKEN not set.\n\n"
    "Set it in your .env file to view live Robinhood data:\n"
    "  RH_AGENTIC_MCP_TOKEN=<your OAuth bearer token>\n\n"
    "Get your token at robinhood.com/us/en/agentic-trading/"
)


class LiveRhScreen(Widget):
    """Live Robinhood account panel — polls MCP every 30 s."""

    def __init__(self) -> None:
        super().__init__(id="live-rh-screen")
        self._accounts: list[dict[str, Any]] = []
        self._portfolios: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, list[dict[str, Any]]] = {}
        self._options: dict[str, list[dict[str, Any]]] = {}
        self._error: str | None = None
        self._last_updated = "never"

    def compose(self) -> ComposeResult:
        with Vertical(classes="screen-body"):
            yield Static(id="live-rh-header", classes="panel")
            with Horizontal(id="live-rh-accounts"):
                yield Static(id="live-rh-main-summary", classes="panel")
                yield Static(id="live-rh-agentic-summary", classes="panel")
            yield DataTable(id="live-rh-positions", classes="panel")
            yield DataTable(id="live-rh-options", classes="panel option-panel")

    def on_mount(self) -> None:
        pos_table = self.query_one("#live-rh-positions", DataTable)
        pos_table.cursor_type = "row"
        pos_table.add_columns("ACCOUNT", "SYMBOL", "QTY", "AVG COST", "TYPE")

        opt_table = self.query_one("#live-rh-options", DataTable)
        opt_table.cursor_type = "row"
        opt_table.add_columns("ACCOUNT", "SYMBOL", "QTY", "AVG PRICE", "EXPIRY", "TYPE")

        self.set_interval(_POLL_SECONDS, self._refresh_worker)
        self.run_worker(self._refresh(), name="live-rh-init", exclusive=True)

    def _refresh_worker(self) -> None:
        self.run_worker(self._refresh(), name="live-rh-poll", exclusive=True)

    async def _refresh(self) -> None:
        from datetime import UTC, datetime

        session = build_session()
        if session._token is None:
            self._error = _NO_TOKEN_MSG
            self._render_error()
            return

        try:
            self._accounts = await fetch_accounts(session)
            self._portfolios = {}
            self._positions = {}
            self._options = {}
            for account in self._accounts:
                acct = str(account.get("account_number") or "")
                if not acct:
                    continue
                self._portfolios[acct] = await fetch_portfolio(session, acct)
                self._positions[acct] = await fetch_equity_positions(session, acct)
                self._options[acct] = await fetch_option_positions(session, acct)
            self._error = None
            self._last_updated = datetime.now(UTC).strftime("%H:%M:%S UTC")
        except Exception as exc:
            self._error = f"MCP error: {exc}"
            self._render_error()
            return

        self._render_header()
        self._render_summaries()
        self._render_positions()
        self._render_options()

    def _render_error(self) -> None:
        self.query_one("#live-rh-header", Static).update(
            f"[bold]Live Robinhood[/bold]  ●  Error\n{self._error}"
        )

    def _render_header(self) -> None:
        n_accounts = len(self._accounts)
        n_positions = sum(len(v) for v in self._positions.values())
        self.query_one("#live-rh-header", Static).update(
            f"[bold]Live Robinhood[/bold]  ●  "
            f"{n_accounts} account(s) · {n_positions} position(s) · "
            f"Updated {self._last_updated}  (auto-refresh every {_POLL_SECONDS}s)"
        )

    def _render_summaries(self) -> None:
        main_acct = next(
            (a for a in self._accounts if not a.get("agentic_allowed")), None
        )
        agentic_acct = next(
            (a for a in self._accounts if a.get("agentic_allowed")), None
        )
        self.query_one("#live-rh-main-summary", Static).update(
            self._account_summary(main_acct, label="Main")
        )
        self.query_one("#live-rh-agentic-summary", Static).update(
            self._account_summary(agentic_acct, label="Agentic (agent trades here)")
        )

    def _account_summary(self, account: dict[str, Any] | None, *, label: str) -> str:
        if account is None:
            return f"[bold]{label}[/bold]\nNo account found."
        acct_num = str(account.get("account_number") or "")
        portfolio = self._portfolios.get(acct_num, {})
        total = dec(portfolio.get("total_value"))
        equity = dec(portfolio.get("equity_value"))
        cash = dec(portfolio.get("cash"))
        crypto = dec(portfolio.get("crypto_value"))
        buying_power = dec(
            (portfolio.get("buying_power") or {}).get("buying_power") if isinstance(portfolio.get("buying_power"), dict) else None
        )
        nickname = account.get("nickname") or account.get("brokerage_account_type") or ""
        agentic_flag = "  [AGENT CAN TRADE]" if account.get("agentic_allowed") else ""
        lines = [
            f"[bold]{label}[/bold]  {mask(acct_num)}{agentic_flag}",
            f"  {nickname} · {account.get('type', '')}",
            f"  Portfolio      ${total:>12,.2f}",
            f"  Equity         ${equity:>12,.2f}",
        ]
        if cash != 0:
            lines.append(f"  Cash           ${cash:>12,.2f}")
        if crypto > 0:
            lines.append(f"  Crypto         ${crypto:>12,.2f}")
        if buying_power > 0:
            lines.append(f"  Buying power   ${buying_power:>12,.2f}")
        n_pos = len(self._positions.get(acct_num, []))
        n_opt = len(self._options.get(acct_num, []))
        lines.append(f"  {n_pos} equity position(s) · {n_opt} option position(s)")
        return "\n".join(lines)

    def _render_positions(self) -> None:
        table = self.query_one("#live-rh-positions", DataTable)
        table.clear()
        any_rows = False
        for account in self._accounts:
            acct_num = str(account.get("account_number") or "")
            positions = self._positions.get(acct_num, [])
            if not positions:
                continue
            label = account.get("nickname") or mask(acct_num)
            agentic = "●" if account.get("agentic_allowed") else " "
            table.add_row(f"{agentic} {label}", "──────", "──────", "──────", "──────", key=f"hdr-{acct_num}")
            for pos in positions:
                qty = dec(pos.get("quantity"))
                avg = dec(pos.get("average_buy_price"))
                symbol = str(pos.get("symbol") or "")
                pos_type = str(pos.get("type") or "long")
                table.add_row(
                    "",
                    symbol,
                    f"{qty:g}",
                    f"${avg:,.2f}" if avg else "—",
                    pos_type,
                    key=f"{acct_num}-{symbol}",
                )
            any_rows = True
        if not any_rows:
            table.add_row("—", "No equity positions", "—", "—", "—")

    def _render_options(self) -> None:
        table = self.query_one("#live-rh-options", DataTable)
        table.clear()
        any_rows = False
        for account in self._accounts:
            acct_num = str(account.get("account_number") or "")
            options = self._options.get(acct_num, [])
            if not options:
                continue
            label = account.get("nickname") or mask(acct_num)
            table.add_row(label, "──────", "──────", "──────", "──────", "──────", key=f"opt-hdr-{acct_num}")
            for pos in options:
                symbol = str(pos.get("chain_symbol") or pos.get("symbol") or "")
                qty = dec(pos.get("quantity"))
                avg = dec(pos.get("average_price"))
                expiry = str(pos.get("expiration_date") or "—")[:10]
                pos_type = str(pos.get("type") or "—")
                table.add_row(
                    "",
                    symbol,
                    f"{qty:g}",
                    f"${avg:,.2f}" if avg else "—",
                    expiry,
                    pos_type,
                    key=f"opt-{acct_num}-{symbol}-{expiry}",
                )
            any_rows = True
        if not any_rows:
            table.add_row("—", "No option positions", "—", "—", "—", "—")

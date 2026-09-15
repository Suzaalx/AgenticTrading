"""History tab scaffold."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from sentinel.core.events import Event, StageChanged
from sentinel.store.db import connect, run_migrations
from sentinel.store.export import export_run_bundle
from sentinel.tui.widgets.common import fetch_rows, money, signed_percent


class HistoryScreen(Widget):
    """Store-backed run history with archived detail and markdown export."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("e", "export_selected", "Export run")]

    def __init__(self) -> None:
        super().__init__(id="history-screen")
        self._run_ids: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="screen-body"):
            with Vertical(classes="panel"):
                yield Static("History runs", classes="panel-title")
                yield DataTable(id="history-runs")
            yield Static(id="history-detail", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#history-runs", DataTable)
        table.cursor_type = "row"
        table.add_columns("DATE", "SYM", "ACTION", "VERDICT", "COST", "DEBATE")
        self.hydrate()

    def hydrate(self) -> None:
        rows = fetch_rows(
            "SELECT run_id, symbol, as_of, action, verdict, cost_usd, status, debate_enabled "
            "FROM runs ORDER BY COALESCE(finished_at, created_at, as_of) DESC LIMIT 50"
        )
        table = self.query_one("#history-runs", DataTable)
        table.clear()
        self._run_ids = []
        for row in rows:
            self._run_ids.append(str(row["run_id"]))
            table.add_row(
                str(row["as_of"] or "")[:10],
                row["symbol"] or "—",
                row["action"] or "—",
                row["verdict"] or row["status"] or "—",
                money(row["cost_usd"]),
                "—" if row["debate_enabled"] is None else ("on" if row["debate_enabled"] else "off"),
                key=row["run_id"],
            )
        self._render_detail(self._run_ids[0] if self._run_ids else None)

    def handle_event(self, event: Event) -> None:
        if isinstance(event, StageChanged) and event.status in {"completed", "failed", "cancelled", "halted"}:
            self.hydrate()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "history-runs":
            self._render_detail(self._selected_run_id())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "history-runs":
            self._render_detail(self._selected_run_id())

    def action_export_selected(self) -> None:
        run_id = self._selected_run_id()
        if run_id is None:
            self.notify("No run selected", severity="warning")
            return
        with connect() as conn:
            run_migrations(conn)
            path = export_run_bundle(conn, run_id)
        self.notify(f"Exported {path}")

    def _selected_run_id(self) -> str | None:
        if not self._run_ids:
            return None
        table = self.query_one("#history-runs", DataTable)
        index = max(0, min(table.cursor_row, len(self._run_ids) - 1))
        return self._run_ids[index]

    def _render_detail(self, run_id: str | None) -> None:
        if run_id is None:
            self.query_one("#history-detail", Static).update("History detail\nNo archived runs.")
            return
        run_rows = fetch_rows("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        report_rows = fetch_rows(
            "SELECT agent, content, cost_usd FROM reports WHERE run_id = ? ORDER BY created_at, agent",
            (run_id,),
        )
        order_rows = fetch_rows(
            "SELECT order_id, side, qty, reason, status FROM orders WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        journal_rows = fetch_rows("SELECT realized_ret, bench_ret, graded FROM journal WHERE run_id = ?", (run_id,))
        if not run_rows:
            self.query_one("#history-detail", Static).update(f"History detail\nMissing run {run_id}")
            return
        run = run_rows[0]
        lines = [
            f"Run {run['run_id']}  {run['symbol']}  {str(run['as_of'] or '')[:10]}",
            f"Status {run['status']}  Action {run['action'] or '—'}  Verdict {run['verdict'] or '—'}  Cost {money(run['cost_usd'])}",
            "",
            "Outcome",
        ]
        if journal_rows:
            journal = journal_rows[0]
            realized = signed_percent(journal["realized_ret"], already_percent=False)
            bench = signed_percent(journal["bench_ret"], already_percent=False)
            lines.append(f"{realized} vs benchmark {bench} — graded {journal['graded'] or 'pending'}")
        else:
            lines.append("No reflection outcome yet.")
        lines.extend(["", "Orders"])
        if order_rows:
            for order in order_rows:
                lines.append(
                    f"{order['order_id']} {order['side']} {order['qty']} {order['reason']} {order['status']}"
                )
        else:
            lines.append("No orders.")
        lines.extend(["", "Reports"])
        if report_rows:
            for report in report_rows:
                content = str(report["content"] or "").replace("\n", " ")
                lines.append(f"[{report['agent']}] {money(report['cost_usd'])}: {content[:240]}")
        else:
            lines.append("No reports.")
        lines.append("\nPress e to export this run bundle to markdown.")
        self.query_one("#history-detail", Static).update("\n".join(lines))

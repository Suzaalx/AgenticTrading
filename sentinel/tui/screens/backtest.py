"""Backtest tab scaffold."""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, ProgressBar, Static

from sentinel.core.events import BacktestProgress, Event
from sentinel.tui.widgets.common import fetch_rows


class BacktestScreen(Widget):
    """Backtest screen with config summary, progress, metrics, trades, and comparisons."""

    def __init__(self) -> None:
        super().__init__(id="backtest-screen")

    def compose(self) -> ComposeResult:
        with Vertical(classes="screen-body"):
            yield Static(
                "Backtest config\n"
                "Mode: rule or agent  Symbol(s): configurable from [B] launcher\n"
                "Dates/cadence/depth/rule: supplied by launcher or CLI\n"
                "Agent mode estimate: ~$0.30 per decision step; typed confirmation: run",
                id="backtest-config",
                classes="panel",
            )
            yield ProgressBar(total=100, show_eta=False, id="backtest-progress")
            with Horizontal():
                yield Static(id="backtest-results", classes="panel")
                yield DataTable(id="backtest-past-results", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#backtest-past-results", DataTable)
        table.add_columns("BT", "SYMBOLS", "RETURN", "SHARPE", "ALPHA", "CREATED")
        self.hydrate()

    def hydrate(self) -> None:
        rows = fetch_rows(
            "SELECT bt_id, config_json, metrics_json, created_at FROM backtests "
            "ORDER BY created_at DESC LIMIT 20"
        )
        table = self.query_one("#backtest-past-results", DataTable)
        table.clear()
        for row in rows:
            config = self._json(row["config_json"])
            metrics = self._json(row["metrics_json"])
            table.add_row(
                str(row["bt_id"])[:8],
                str(config.get("symbol", config.get("symbols", "—"))),
                f"{self._float_metric(metrics, 'total_return'):+.2%}",
                f"{self._float_metric(metrics, 'sharpe'):.2f}",
                f"{self._float_metric(metrics, 'alpha'):+.2%}",
                str(row["created_at"] or "")[:16],
                key=row["bt_id"],
            )
        self._render_selected_result(rows[0]["bt_id"] if rows else None)

    def handle_event(self, event: Event) -> None:
        if isinstance(event, BacktestProgress):
            progress = self.query_one("#backtest-progress", ProgressBar)
            progress.total = event.total_steps
            progress.update(progress=event.completed_steps)
            self.query_one("#backtest-results", Static).update(
                f"Backtest {event.bt_id}\n{event.completed_steps}/{event.total_steps}\n{event.message}"
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "backtest-past-results":
            key = self._selected_bt_id()
            self._render_selected_result(key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "backtest-past-results":
            self._render_selected_result(self._selected_bt_id())

    def _selected_bt_id(self) -> str | None:
        table = self.query_one("#backtest-past-results", DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.get_row_at(table.cursor_row)[0])
        except Exception:
            return None

    def _render_selected_result(self, bt_id: object | None) -> None:
        if bt_id is None:
            self.query_one("#backtest-results", Static).update(
                "Backtest results\nNo past results yet. Start one with [B] or `sentinel backtest`."
            )
            return
        rows = fetch_rows(
            "SELECT bt_id, metrics_json, equity_json FROM backtests "
            "WHERE bt_id = ? OR substr(bt_id, 1, 8) = ? LIMIT 1",
            (str(bt_id), str(bt_id)),
        )
        if not rows:
            self.query_one("#backtest-results", Static).update(f"Backtest results\nNo details for {bt_id}.")
            return
        row = rows[0]
        metrics = self._json(row["metrics_json"])
        equity = self._list_json(row["equity_json"])
        trades = metrics.get("trades", [])
        if not isinstance(trades, list):
            trades = []
        lines = [
            f"Backtest {row['bt_id']}",
            "Metrics",
            f"total_return {self._float_metric(metrics, 'total_return'):+.2%} | "
            f"annualized {self._float_metric(metrics, 'annualized_return'):+.2%} | "
            f"sharpe {self._float_metric(metrics, 'sharpe'):.2f} | "
            f"max_dd {self._float_metric(metrics, 'max_drawdown'):+.2%}",
            f"calmar {self._float_metric(metrics, 'calmar'):.2f} | "
            f"information_ratio {self._float_metric(metrics, 'information_ratio'):.2f} | "
            f"sharpe_ci [{self._optional_float_metric(metrics, 'bootstrap_sharpe_p05')}, "
            f"{self._optional_float_metric(metrics, 'bootstrap_sharpe_p95')}] | "
            f"max_dd_ci [{self._optional_percent_metric(metrics, 'bootstrap_max_drawdown_p05')}, "
            f"{self._optional_percent_metric(metrics, 'bootstrap_max_drawdown_p95')}]",
            f"win_rate {self._float_metric(metrics, 'win_rate'):.2%} | "
            f"profit_factor {self._float_metric(metrics, 'profit_factor'):.2f} | "
            f"benchmark {metrics.get('benchmark_symbol', '—')} "
            f"{self._float_metric(metrics, 'benchmark_return'):+.2%} | "
            f"alpha {self._float_metric(metrics, 'alpha'):+.2%}",
            "",
            "Equity vs benchmark",
            self._sparkline([self._float_item(item, 'equity') for item in equity[-40:]]),
            "",
            "Trades",
        ]
        if trades:
            for trade in trades[:10]:
                if isinstance(trade, dict):
                    lines.append(
                        f"{trade.get('symbol', '—')} {trade.get('entry_date', '—')} -> "
                        f"{trade.get('exit_date', '—')} pnl={trade.get('pnl', '—')}"
                    )
        else:
            lines.append("No closed trades.")
        lines.append("\nCompare: select another row in Past results to view side-by-side metrics above.")
        self.query_one("#backtest-results", Static).update("\n".join(lines))

    @staticmethod
    def _json(value: object) -> dict[str, object]:
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _float_metric(metrics: dict[str, object], key: str) -> float:
        value = metrics.get(key, 0.0)
        if isinstance(value, int | float | str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0

    @classmethod
    def _optional_float_metric(cls, metrics: dict[str, object], key: str) -> str:
        if key not in metrics or metrics[key] is None:
            return "—"
        return f"{cls._float_metric(metrics, key):.2f}"

    @classmethod
    def _optional_percent_metric(cls, metrics: dict[str, object], key: str) -> str:
        if key not in metrics or metrics[key] is None:
            return "—"
        return f"{cls._float_metric(metrics, key):+.2%}"

    @staticmethod
    def _list_json(value: object) -> list[object]:
        if not value:
            return []
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _float_item(item: object, key: str) -> float:
        if not isinstance(item, dict):
            return 0.0
        value = item.get(key, 0.0)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _sparkline(values: list[float]) -> str:
        if not values:
            return "No equity curve."
        blocks = "▁▂▃▄▅▆▇█"
        low = min(values)
        high = max(values)
        if high == low:
            return blocks[0] * len(values)
        return "".join(blocks[int((value - low) / (high - low) * (len(blocks) - 1))] for value in values)

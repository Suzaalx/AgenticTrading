"""CommandService implementation backed by the Sentinel orchestrator."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sentinel.config.settings import Settings, load_mandate, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.commands import Depth
from sentinel.core.events import EquityUpdated, KillSwitchChanged, QuoteTick
from sentinel.core.ids import new_id
from sentinel.core.models import Mandate, Order, Quote
from sentinel.data.router import DataRouter
from sentinel.execution.broker import OrderRejected, QuoteSource
from sentinel.execution.ledger import append_order, record_fill, update_order_status
from sentinel.execution.paper import PaperBroker
from sentinel.execution.portfolio import (
    append_equity_snapshot,
    apply_fill,
    load_portfolio,
    load_positions,
    save_positions,
)
from sentinel.llm.contracts import StructuredLLM
from sentinel.llm.gateway import LLMGateway
from sentinel.memory.reflection_job import run_reflection_job_async
from sentinel.risk._events import set_event_bus
from sentinel.risk.gate import check_order
from sentinel.risk.killswitch import disengage, engage, is_engaged
from sentinel.risk.monitor import PositionMonitor
from sentinel.store.db import connect, default_db_path, run_migrations

from .graph import RouterQuoteSource
from .runner import OrchestratorRunner
from .scheduler import DecisionScheduler


class SentinelCommandService:
    """Single command surface shared by CLI and TUI."""

    def __init__(
        self,
        *,
        bus: EventBus,
        conn: sqlite3.Connection,
        settings: Settings,
        mandate: Mandate,
        llm: StructuredLLM | None = None,
        router: DataRouter | None = None,
        quote_source: QuoteSource | None = None,
        broker: PaperBroker | None = None,
    ) -> None:
        self.event_bus = bus
        set_event_bus(bus)
        self.conn = conn
        self.settings = settings
        self.mandate = mandate
        self.router = router or DataRouter(settings=settings)
        self.quote_source = quote_source or RouterQuoteSource(self.router)
        self.broker = broker or PaperBroker(self.quote_source, bus=bus)
        self.runner = OrchestratorRunner(
            bus=bus,
            conn=conn,
            settings=settings,
            mandate=mandate,
            llm=llm or LLMGateway(settings=settings),
            router=self.router,
            quote_source=self.quote_source,
            broker=self.broker,
        )
        self._tasks: dict[str, asyncio.Task[object]] = {}
        self._monitor: PositionMonitor | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._monitor_stop: asyncio.Event | None = None
        self._scheduler: DecisionScheduler | None = None

    def get_event_bus(self) -> EventBus:
        """Return the event bus for TUI subscription injection."""

        return self.event_bus

    async def start_run(self, symbol: str, date: date | None, depth: Depth) -> str:
        """Start a decision run and return its run id immediately."""

        as_of = (
            datetime.combine(date, datetime.min.time(), tzinfo=UTC)
            if date is not None
            else datetime.now(UTC)
        )
        run_id = new_id()
        task = asyncio.create_task(
            self.runner.run(symbol, as_of=as_of, depth=depth, run_id=run_id),
            name=f"sentinel-run-{run_id}",
        )
        self._tasks[run_id] = task
        return run_id

    async def cancel_run(self, run_id: str) -> None:
        self.runner.cancel_run(run_id)

    async def toggle_kill(self, on: bool) -> None:
        if on:
            engage(actor="command_service")
        else:
            disengage(actor="command_service")
        await self.event_bus.publish(KillSwitchChanged(enabled=is_engaged(), actor="command_service"))

    async def close_position(self, symbol: str) -> None:
        """Submit a mechanical manual close order through gate and paper broker."""

        portfolio = load_portfolio(self.conn, starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)))
        position = next((item for item in portfolio.positions if item.symbol == symbol.upper()), None)
        if position is None or position.qty <= 0:
            return
        order = Order(
            order_id=new_id(),
            run_id=None,
            symbol=symbol.upper(),
            side="sell",
            qty=position.qty,
            type="market",
            reason="manual",
            created_at=datetime.now(UTC),
        )
        await self.submit_exit_order(order)

    async def submit_exit_order(self, order: Order) -> None:
        """Gate, execute, persist, and publish a mechanical risk-reducing exit order."""

        portfolio = load_portfolio(
            self.conn,
            starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)),
        )
        quote = await self._get_quote(order.symbol)
        marks = {order.symbol: quote.price} if quote is not None else None
        result = check_order(
            order,
            portfolio,
            self.mandate,
            orders_today=self._orders_today(order.created_at),
            last_order_ts=self._last_order_ts_by_symbol(),
            kill_engaged=is_engaged(),
            marks=marks,
            day_pnl_pct=None,
        )
        if not result.passed:
            return
        append_order(self.conn, order, status="submitted")
        fill = await self.broker.submit(order)
        if isinstance(fill, OrderRejected):
            update_order_status(self.conn, order.order_id, "rejected")
            return
        record_fill(self.conn, order, fill)
        updated, _ = apply_fill(portfolio, order, fill)
        save_positions(self.conn, updated)
        snapshot = append_equity_snapshot(self.conn, updated, marks={order.symbol: fill.price})
        await self.event_bus.publish(
            EquityUpdated(equity=snapshot.equity, cash=snapshot.cash, day_pnl=snapshot.day_pnl)
        )

    async def start_backtest(self, config: dict[str, Any]) -> str:
        """Start an agent replay backtest using this orchestrator's single-step seam."""

        from sentinel.backtest.engine import BacktestConfig, run_backtest_async

        bt_id = str(config.get("bt_id") or f"bt_{new_id()}")
        cfg = BacktestConfig.from_mapping({**config, "bt_id": bt_id, "mode": config.get("mode", "agent")})
        task = asyncio.create_task(
            run_backtest_async(cfg, pipeline=self.runner.run_backtest_step, event_bus=self.event_bus),
            name=f"sentinel-backtest-{bt_id}",
        )
        self._tasks[bt_id] = task
        return bt_id

    async def reflect_now(self) -> None:
        await run_reflection_job_async(self.conn, bus=self.event_bus)

    async def run_position_monitor_once(self, now: datetime | None = None) -> list[Order]:
        """Evaluate stops once; testable seam used by the app's background monitor."""

        monitor = self._position_monitor()
        return await monitor.run_once(now=now)

    def start_background_services(self) -> None:
        """Start app-scoped background services: stop monitor and optional cron scheduler."""

        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_stop = asyncio.Event()
            self._monitor = self._position_monitor()
            self._monitor_task = asyncio.create_task(
                self._monitor.run_forever(self._monitor_stop),
                name="sentinel-position-monitor",
            )
        if self.settings.schedule.enabled:
            if self._scheduler is None:
                self._scheduler = DecisionScheduler(self)
            self._scheduler.start()

    async def stop_background_services(self) -> None:
        """Stop app-scoped background services."""

        if self._scheduler is not None:
            await self._scheduler.stop()
        if self._monitor_stop is not None:
            self._monitor_stop.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task

    def _position_monitor(self) -> PositionMonitor:
        interval_min = min(
            self.settings.monitor.equity_interval_min,
            self.settings.monitor.crypto_interval_min,
        )
        return PositionMonitor(
            positions_provider=lambda: load_positions(self.conn),
            marks_provider=self._marks_for_open_positions,
            on_order=self.submit_exit_order,
            interval_seconds=float(interval_min * 60),
        )

    async def _marks_for_open_positions(self) -> dict[str, Decimal]:
        marks: dict[str, Decimal] = {}
        for position in load_positions(self.conn):
            quote = await self._get_quote(position.symbol)
            if quote is None:
                continue
            marks[position.symbol] = quote.price
            await self.event_bus.publish(QuoteTick(quote=quote))
        return marks

    async def _get_quote(self, symbol: str) -> Quote | None:
        quote = self.quote_source.get_quote(symbol)
        if hasattr(quote, "__await__"):
            quote = await quote  # type: ignore[assignment]
        return quote

    def _orders_today(self, now: datetime) -> int:
        prefix = now.date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE substr(created_at, 1, 10) = ?",
            (prefix,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _last_order_ts_by_symbol(self) -> dict[str, datetime]:
        rows = self.conn.execute(
            "SELECT symbol, MAX(created_at) AS ts FROM orders GROUP BY symbol",
        ).fetchall()
        return {
            str(row["symbol"]): datetime.fromisoformat(str(row["ts"]))
            for row in rows
            if row["ts"] is not None
        }


def build_command_service(
    bus: EventBus | None = None,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    mandate: Mandate | None = None,
    *,
    llm: StructuredLLM | None = None,
    router: DataRouter | None = None,
    quote_source: QuoteSource | None = None,
    broker: PaperBroker | None = None,
) -> SentinelCommandService:
    """Factory used by CLI/TUI bootstrap to share one orchestrator service."""

    resolved_bus = bus or EventBus()
    resolved_settings = settings or load_settings(Path.cwd())
    resolved_mandate = mandate or load_mandate(Path.cwd())
    resolved_conn = conn or connect(default_db_path())
    run_migrations(resolved_conn)
    return SentinelCommandService(
        bus=resolved_bus,
        conn=resolved_conn,
        settings=resolved_settings,
        mandate=resolved_mandate,
        llm=llm,
        router=router,
        quote_source=quote_source,
        broker=broker,
    )

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.models import Mandate, Portfolio, Position, Quote
from sentinel.execution.paper import PaperBroker
from sentinel.execution.portfolio import append_equity_snapshot, save_positions
from sentinel.orchestrator.service import build_command_service
from sentinel.store.db import connect, run_migrations


class FakeLLM:
    async def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LLM should not be called by mechanical exits")


class StaticQuoteSource:
    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, price=Decimal("89"), ts=datetime.now(UTC), source="test")


@pytest.mark.asyncio
async def test_position_monitor_stop_loss_executes_mechanical_exit_in_paper() -> None:
    conn = connect()
    run_migrations(conn)
    settings = Settings()
    mandate = Mandate(
        symbol_universe=["NVDA"],
        max_position_pct_equity=100.0,
        max_order_notional_usd=10_000.0,
        max_gross_exposure_pct=100.0,
        max_daily_loss_pct=1.0,
        max_orders_per_day=0,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
    )
    opened_at = datetime.now(UTC) - timedelta(days=1)
    position = Position(
        symbol="NVDA",
        qty=Decimal("5"),
        avg_cost=Decimal("100"),
        stop_loss_pct=10.0,
        take_profit_pct=None,
        opened_at=opened_at,
        horizon_days=30,
        source_run_id="run-stop",
    )
    portfolio = Portfolio(cash=Decimal("9500"), positions=[position], day_pnl=Decimal("-500"))
    save_positions(conn, portfolio)
    append_equity_snapshot(conn, portfolio, marks={"NVDA": Decimal("100")}, ts=opened_at)
    quote_source = StaticQuoteSource()
    bus = EventBus()
    service = build_command_service(
        bus,
        conn,
        settings,
        mandate,
        llm=FakeLLM(),  # type: ignore[arg-type]
        quote_source=quote_source,  # type: ignore[arg-type]
        broker=PaperBroker(quote_source, bus=bus, slippage_bps=0),
    )

    orders = await service.run_position_monitor_once(datetime.now(UTC))

    assert len(orders) == 1
    assert orders[0].reason == "stop_loss"
    order_row = conn.execute("SELECT reason, status FROM orders WHERE order_id = ?", (orders[0].order_id,)).fetchone()
    assert dict(order_row) == {"reason": "stop_loss", "status": "filled"}
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0

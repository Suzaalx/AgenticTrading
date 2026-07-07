from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.bus import EventBus
from sentinel.core.events import OrderFilled, OrderSubmitted
from sentinel.core.models import Order, Quote
from sentinel.execution.broker import OrderRejected
from sentinel.execution.paper import PaperBroker


class FakeQuoteSource:
    def __init__(self, quote: Quote | None) -> None:
        self.quote = quote

    def get_quote(self, symbol: str) -> Quote:
        if self.quote is None:
            raise LookupError(symbol)
        return self.quote


def order(side: str = "buy") -> Order:
    return Order(
        order_id=f"order-{side}",
        run_id="run-1",
        symbol="NVDA",
        side=side,  # type: ignore[arg-type]
        qty=Decimal("2"),
        type="market",
        reason="agent_decision",
        created_at=datetime(2026, 7, 6, 14, 31, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_paper_broker_buy_slippage_commission_and_events() -> None:
    now = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    bus = EventBus()
    queue = await bus.subscribe_queue()
    broker = PaperBroker(
        FakeQuoteSource(Quote(symbol="NVDA", price=Decimal("100"), ts=now, source="fake")),
        bus=bus,
        slippage_bps=5,
        commission_usd=Decimal("1.25"),
        clock=lambda: now,
    )

    result = await broker.submit(order("buy"))

    assert not isinstance(result, OrderRejected)
    assert result.price == Decimal("100.05")
    assert result.slippage_usd == Decimal("0.10")
    assert result.commission_usd == Decimal("1.25")
    assert isinstance(await asyncio.wait_for(queue.get(), timeout=1), OrderSubmitted)
    assert isinstance(await asyncio.wait_for(queue.get(), timeout=1), OrderFilled)


@pytest.mark.asyncio
async def test_paper_broker_sell_slippage() -> None:
    now = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    broker = PaperBroker(
        FakeQuoteSource(Quote(symbol="NVDA", price=Decimal("100"), ts=now, source="fake")),
        slippage_bps=5,
        commission_usd=Decimal("0"),
        clock=lambda: now,
    )

    result = await broker.submit(order("sell"))

    assert not isinstance(result, OrderRejected)
    assert result.price == Decimal("99.95")
    assert result.slippage_usd == Decimal("0.10")


@pytest.mark.asyncio
async def test_paper_broker_rejects_absent_quote() -> None:
    broker = PaperBroker(FakeQuoteSource(None), clock=lambda: datetime(2026, 7, 6, 15, 0, tzinfo=UTC))

    result = await broker.submit(order())

    assert isinstance(result, OrderRejected)
    assert "no quote" in result.reason


@pytest.mark.asyncio
async def test_paper_broker_rejects_stale_quote_during_market_hours() -> None:
    now = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    broker = PaperBroker(
        FakeQuoteSource(
            Quote(symbol="NVDA", price=Decimal("100"), ts=now - timedelta(minutes=31), source="fake")
        ),
        clock=lambda: now,
    )

    result = await broker.submit(order())

    assert isinstance(result, OrderRejected)
    assert "stale" in result.reason

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.models import Order, Quote
from sentinel.execution.broker import OrderRejected
from sentinel.execution.paper import PaperBroker


class FakeQuoteSource:
    def __init__(self, quote: Quote) -> None:
        self.quote = quote

    def get_quote(self, symbol: str) -> Quote:
        return self.quote.model_copy(update={"symbol": symbol})


@pytest.mark.asyncio
async def test_paper_broker_records_quote_timestamp_used_for_fill() -> None:
    now = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)
    quote_ts = now - timedelta(minutes=5)
    broker = PaperBroker(
        FakeQuoteSource(Quote(symbol="NVDA", price=Decimal("100"), ts=quote_ts, source="fake")),
        clock=lambda: now,
    )
    order = Order(
        order_id="order-quote-ts",
        run_id="run-1",
        symbol="NVDA",
        side="buy",
        qty=Decimal("1"),
        type="market",
        reason="agent_decision",
        created_at=now,
    )

    result = await broker.submit(order)

    assert not isinstance(result, OrderRejected)
    assert broker.quote_ts_for_order(order.order_id) == quote_ts

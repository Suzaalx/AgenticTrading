from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sentinel.core.models import Fill, Order, Portfolio
from sentinel.execution.portfolio import apply_fill


def make_order(order_id: str, side: str, qty: str) -> Order:
    return Order(
        order_id=order_id,
        run_id="run-1",
        symbol="MSFT",
        side=side,  # type: ignore[arg-type]
        qty=Decimal(qty),
        type="market",
        reason="manual",
        created_at=datetime(2026, 7, 6, 14, 30, tzinfo=UTC),
    )


def make_fill(order_id: str, price: str, qty: str, commission: str = "0") -> Fill:
    return Fill(
        order_id=order_id,
        price=Decimal(price),
        qty=Decimal(qty),
        ts=datetime(2026, 7, 6, 15, 0, tzinfo=UTC),
        slippage_usd=Decimal("0"),
        commission_usd=Decimal(commission),
    )


def test_weighted_average_cost_across_multiple_buys() -> None:
    portfolio = Portfolio(cash=Decimal("10000"))
    portfolio, _ = apply_fill(portfolio, make_order("o1", "buy", "2"), make_fill("o1", "100", "2"))
    portfolio, _ = apply_fill(portfolio, make_order("o2", "buy", "3"), make_fill("o2", "110", "3"))

    assert len(portfolio.positions) == 1
    assert portfolio.positions[0].qty == Decimal("5")
    assert portfolio.positions[0].avg_cost == Decimal("106")


def test_partial_sell_reduces_qty_and_realizes_pnl() -> None:
    portfolio = Portfolio(cash=Decimal("10000"))
    portfolio, _ = apply_fill(portfolio, make_order("o1", "buy", "2"), make_fill("o1", "100", "2"))
    portfolio, _ = apply_fill(portfolio, make_order("o2", "buy", "3"), make_fill("o2", "110", "3"))

    portfolio, realized = apply_fill(
        portfolio,
        make_order("o3", "sell", "2"),
        make_fill("o3", "120", "2", commission="1"),
    )

    assert realized == Decimal("27")
    assert len(portfolio.positions) == 1
    assert portfolio.positions[0].qty == Decimal("3")
    assert portfolio.positions[0].avg_cost == Decimal("106")

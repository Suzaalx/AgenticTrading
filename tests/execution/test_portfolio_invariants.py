from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sentinel.core.models import Fill, Order, Portfolio
from sentinel.execution.portfolio import apply_fill


def make_order(order_id: str, side: str, qty: Decimal) -> Order:
    return Order(
        order_id=order_id,
        run_id="run-1",
        symbol="NVDA",
        side=side,  # type: ignore[arg-type]
        qty=qty,
        type="market",
        reason="agent_decision",
        created_at=datetime(2026, 7, 6, 14, 30, tzinfo=UTC),
    )


def make_fill(order_id: str, price: str, qty: Decimal, commission: str, slippage: str) -> Fill:
    return Fill(
        order_id=order_id,
        price=Decimal(price),
        qty=qty,
        ts=datetime(2026, 7, 6, 15, 0, tzinfo=UTC),
        slippage_usd=Decimal(slippage),
        commission_usd=Decimal(commission),
    )


def test_cash_plus_positions_value_equals_equity_after_valid_sequence() -> None:
    portfolio = Portfolio(cash=Decimal("10000"))
    sequence = [
        ("o1", "buy", Decimal("10"), "100.05", "1", "0.50"),
        ("o2", "buy", Decimal("5"), "101.05", "1", "0.25"),
        ("o3", "sell", Decimal("8"), "109.945", "1", "0.44"),
    ]

    for order_id, side, qty, price, commission, slippage in sequence:
        portfolio, _ = apply_fill(
            portfolio,
            make_order(order_id, side, qty),
            make_fill(order_id, price, qty, commission, slippage),
        )
        assert portfolio.cash >= Decimal("0")
        assert portfolio.equity == portfolio.cash + sum(
            position.qty * position.avg_cost for position in portfolio.positions
        )


def test_buy_then_sell_round_trip_realized_pnl_equals_price_diff_minus_costs() -> None:
    qty = Decimal("2")
    buy_quote = Decimal("100")
    sell_quote = Decimal("110")
    buy_slippage = Decimal("0.10")
    sell_slippage = Decimal("0.11")
    buy_commission = Decimal("1.25")
    sell_commission = Decimal("1.25")
    portfolio = Portfolio(cash=Decimal("10000"))

    portfolio, buy_realized = apply_fill(
        portfolio,
        make_order("buy", "buy", qty),
        make_fill("buy", "100.05", qty, str(buy_commission), str(buy_slippage)),
    )
    portfolio, sell_realized = apply_fill(
        portfolio,
        make_order("sell", "sell", qty),
        make_fill("sell", "109.945", qty, str(sell_commission), str(sell_slippage)),
    )

    expected = (sell_quote - buy_quote) * qty - buy_slippage - sell_slippage
    expected -= buy_commission + sell_commission
    assert buy_realized + sell_realized == expected
    assert portfolio.positions == []
    assert portfolio.cash == Decimal("10000") + expected

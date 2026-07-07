from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sentinel.core.models import Fill, Order
from sentinel.execution.ledger import append_fill, append_order, get_fills, get_order
from sentinel.store.db import connect, run_migrations, sentinel_home


def test_orders_and_fills_round_trip_as_text_decimals() -> None:
    conn = connect(sentinel_home() / "execution-ledger.db")
    run_migrations(conn)
    now = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    order = Order(
        order_id="order-1",
        run_id=None,
        symbol="NVDA",
        side="buy",
        qty=Decimal("0.123456789123456789"),
        type="market",
        reason="manual",
        created_at=now,
    )
    fill = Fill(
        order_id="order-1",
        price=Decimal("123.456789123456789"),
        qty=Decimal("0.123456789123456789"),
        ts=now,
        slippage_usd=Decimal("0.000000000000000001"),
        commission_usd=Decimal("1.000000000000000001"),
    )

    append_order(conn, order)
    append_fill(conn, fill)

    assert get_order(conn, "order-1") == order
    assert get_fills(conn, order_id="order-1") == [fill]
    order_type = conn.execute("SELECT typeof(qty) FROM orders WHERE order_id = ?", ("order-1",)).fetchone()[0]
    fill_row = conn.execute(
        "SELECT typeof(price), typeof(qty), typeof(slippage_usd), typeof(commission_usd) FROM fills"
    ).fetchone()
    assert order_type == "text"
    assert tuple(fill_row) == ("text", "text", "text", "text")

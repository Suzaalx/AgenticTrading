
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.core.bus import EventBus
from sentinel.core.events import ReconciliationFailed
from sentinel.core.models import Fill, Order
from sentinel.execution.broker import OrderPending, OrderRejected, Venue
from sentinel.execution.ledger import append_fill, append_order, get_fills
from sentinel.execution.portfolio import load_portfolio
from sentinel.execution.reconcile import Reconciler, ReconciliationStatus
from sentinel.risk.audit import read_audit
from sentinel.store.db import connect, run_migrations

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def conn(tmp_path: Path) -> sqlite3.Connection:
    active = connect(tmp_path / "sentinel.db")
    run_migrations(active)
    return active


def order(**kwargs: object) -> Order:
    data = {
        "order_id": "order-1",
        "run_id": "run-1",
        "symbol": "BTC-USD",
        "side": "buy",
        "qty": Decimal("10"),
        "type": "limit",
        "reason": "agent_decision",
        "created_at": NOW,
        "asset_type": "crypto",
        "venue": "robinhood_crypto",
        "client_order_id": "client-1",
    }
    data.update(kwargs)
    return Order(**data)  # type: ignore[arg-type]


@dataclass
class BrokerOrder:
    client_order_id: str | None = "client-1"
    status: str = "working"
    broker_order_id: str = "broker-1"
    venue: Venue = "robinhood_crypto"
    filled_qty: Decimal | None = Decimal("0")
    avg_fill_price: Decimal | None = None
    executions: list[dict[str, object]] | None = None
    order: Order | str | None = None


class FakeBroker:
    def __init__(self) -> None:
        self.orders: list[object] = []
        self.positions_data: object = []
        self.cancelled: list[str] = []
        self.cancel_result = True
        self.cash = Decimal("10000")
        self.submitted: list[Order] = []

    async def submit(self, submitted_order: Order) -> Fill | OrderRejected | OrderPending:
        self.submitted.append(submitted_order)
        return OrderPending(client_order_id=submitted_order.client_order_id, venue="robinhood_crypto")

    async def cancel(self, client_order_id: str) -> bool:
        self.cancelled.append(client_order_id)
        return self.cancel_result

    async def open_orders(self) -> list[object]:
        return self.orders

    async def positions(self) -> object:
        return self.positions_data

    def capabilities(self) -> set[str]:
        return {"crypto"}


def seed_live_order(active: sqlite3.Connection, active_order: Order, *, submitted_at: datetime = NOW) -> None:
    append_order(active, active_order, status="submitted")
    active.execute(
        """INSERT INTO live_orders
        (client_order_id, broker_order_id, venue, status, submitted_at, last_sync_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            active_order.client_order_id,
            "broker-1",
            active_order.venue,
            "working",
            submitted_at.isoformat(),
            None,
            json.dumps({"order": active_order.model_dump(mode="json")}),
        ),
    )
    active.commit()


@pytest.mark.asyncio
async def test_partial_fill_uses_execution_slice_price_after_prior_fill(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order(qty=Decimal("10"))
    seed_live_order(active, active_order)
    append_fill(
        active,
        Fill(
            order_id=active_order.order_id,
            price=Decimal("10"),
            qty=Decimal("4"),
            ts=NOW,
            slippage_usd=Decimal("0"),
            commission_usd=Decimal("0"),
            venue="robinhood_crypto",
        ),
    )
    active.execute(
        """INSERT INTO positions
        (symbol, qty, avg_cost, stop_pct, tp_pct, horizon_days, opened_at, source_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("BTC-USD", "4", "10", None, None, None, NOW.isoformat(), "run-1"),
    )
    active.execute(
        "INSERT INTO equity_curve (ts, equity, cash, day_pnl) VALUES (?, ?, ?, ?)",
        (NOW.isoformat(), "10000", "9960", "0"),
    )
    active.commit()
    broker = FakeBroker()
    broker.orders = [
        BrokerOrder(
            status="partial",
            filled_qty=Decimal("7"),
            executions=[
                {"qty": "4", "price": "10"},
                {"qty": "2", "price": "20"},
                {"quantity": "1", "average_price": "26"},
            ],
            order=active_order,
        )
    ]
    broker.positions_data = {"positions": [{"asset_code": "btc", "quantity": "7"}], "cash_usd": "9894"}

    result = await Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW).run_once()

    fills = get_fills(active, order_id=active_order.order_id)
    portfolio = load_portfolio(active)
    assert result.ok
    assert fills[-1].price == Decimal("22")
    assert fills[-1].qty == Decimal("3")
    assert portfolio.positions[0].qty == Decimal("7")


@pytest.mark.asyncio
async def test_missing_client_order_and_missing_fill_price_are_skipped_and_audited(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order()
    seed_live_order(active, active_order)
    broker = FakeBroker()
    broker.orders = [
        BrokerOrder(client_order_id=None, status="working"),
        BrokerOrder(status="partial", filled_qty=Decimal("2"), order=active_order),
    ]

    await Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW).run_once()

    assert get_fills(active, order_id=active_order.order_id) == []
    assert any(row["kind"] == "live_fill_skipped_missing_price" for row in read_audit())


@pytest.mark.asyncio
async def test_ttl_cancel_failure_marks_order_rejected(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order()
    seed_live_order(active, active_order, submitted_at=NOW - timedelta(hours=1))
    broker = FakeBroker()
    broker.cancel_result = False

    await Reconciler(
        active,
        brokers={"robinhood_crypto": broker},
        live_order_ttl=timedelta(minutes=30),
        clock=lambda: NOW,
    ).run_once()

    assert broker.cancelled == [active_order.client_order_id]
    assert active.execute("SELECT status FROM live_orders").fetchone()["status"] == "rejected"


@pytest.mark.asyncio
async def test_adopt_broker_resolves_position_and_cash_mismatch_with_cash_row(tmp_path: Path) -> None:
    active = conn(tmp_path)
    broker = FakeBroker()
    broker.positions_data = [
        {"asset": "eth", "qty": "2"},
        {"symbol": "CASH", "quantity": "1234.56"},
    ]
    reconciler = Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW)

    before = await reconciler.run_once()
    after = await reconciler.run_once(adopt_broker=True)
    portfolio = load_portfolio(active)

    assert not before.ok
    assert before.results[0].diff["positions"] == {"ETH-USD": {"broker_qty": "2", "mirror_qty": "0"}}
    assert after.ok
    assert portfolio.cash == Decimal("1234.56")
    assert portfolio.positions[0].symbol == "ETH-USD"
    assert portfolio.positions[0].qty == Decimal("2")


@pytest.mark.asyncio
async def test_reconciliation_failure_publishes_and_status_stale_for_naive_timestamp(tmp_path: Path) -> None:
    active = conn(tmp_path)
    broker = FakeBroker()
    broker.positions_data = {"positions": [{"symbol": "BTC-USD", "qty": "1"}], "buying_power": "9999"}
    bus = EventBus()
    queue = await bus.subscribe_queue()

    result = await Reconciler(active, brokers={"robinhood_crypto": broker}, bus=bus, clock=lambda: NOW).run_once()
    event = await queue.get()
    stale = ReconciliationStatus(
        active,
        interval_seconds=300,
        clock=lambda: NOW + timedelta(minutes=20),
    ).context()

    assert not result.ok
    assert isinstance(event, ReconciliationFailed)
    assert stale == {"reconciliation_stale": True, "reconciliation_age_ticks": 4}

    active.execute(
        "UPDATE reconciliations SET ts = ?",
        ((NOW - timedelta(minutes=20)).replace(tzinfo=None).isoformat(),),
    )
    active.commit()
    assert ReconciliationStatus(active, interval_seconds=300, clock=lambda: NOW).context()["reconciliation_stale"] is True


@pytest.mark.asyncio
async def test_order_lookup_handles_raw_order_json_string_and_bad_json(tmp_path: Path) -> None:
    active = conn(tmp_path)
    good_order = order(client_order_id="client-good")
    bad_order = order(order_id="bad", client_order_id="client-bad")
    seed_live_order(active, good_order)
    seed_live_order(active, bad_order)
    active.execute(
        "UPDATE live_orders SET raw_json = ? WHERE client_order_id = ?",
        (json.dumps({"order": good_order.model_dump_json()}), good_order.client_order_id),
    )
    active.execute(
        "UPDATE live_orders SET raw_json = ? WHERE client_order_id = ?",
        ("{not-json", bad_order.client_order_id),
    )
    active.commit()
    broker = FakeBroker()
    broker.orders = [
        BrokerOrder(client_order_id="client-good", status="filled", filled_qty=Decimal("1"), avg_fill_price=Decimal("5")),
        BrokerOrder(client_order_id="client-bad", status="filled", filled_qty=Decimal("1"), avg_fill_price=Decimal("5")),
    ]
    broker.positions_data = {"positions": [{"symbol": "BTC-USD", "qty": "1"}], "cash": "9995"}

    await Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW).run_once()

    assert len(get_fills(active, order_id=good_order.order_id)) == 1
    assert get_fills(active, order_id=bad_order.order_id) == []

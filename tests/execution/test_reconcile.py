from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.events import EquityUpdated, OrderFilled, ReconciliationFailed
from sentinel.core.models import Fill, Mandate, MandateLive, OptionContract, OptionLeg, Order
from sentinel.execution.broker import OrderPending, OrderRejected, Venue
from sentinel.execution.ledger import append_order, get_fills
from sentinel.execution.portfolio import load_option_positions, load_portfolio
from sentinel.execution.reconcile import Reconciler, ReconciliationStatus
from sentinel.execution.router import ExecutionRouter
from sentinel.orchestrator.service import build_command_service
from sentinel.risk.audit import read_audit
from sentinel.risk.live_mandate import LiveMandate
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
        "qty": Decimal("5"),
        "type": "limit",
        "reason": "agent_decision",
        "created_at": NOW,
        "asset_type": "crypto",
        "venue": "robinhood_crypto",
    }
    data.update(kwargs)
    return Order(**data)  # type: ignore[arg-type]


def option_order(**kwargs: object) -> Order:
    contracts = int(kwargs.pop("contracts", 1))
    data = {
        "order_id": "option-order-1",
        "run_id": "run-1",
        "symbol": "NVDA",
        "side": "buy",
        "qty": Decimal(contracts),
        "type": "net_debit",
        "reason": "agent_decision",
        "created_at": NOW,
        "asset_type": "option",
        "venue": "robinhood_agentic",
        "strategy": "long_call",
        "legs": [
            OptionLeg(
                contract=OptionContract(
                    contract_symbol="NVDA260717C00100000",
                    underlying="NVDA",
                    kind="call",
                    strike=Decimal("100"),
                    expiry=date(2026, 7, 17),
                ),
                side="buy",
                contracts=contracts,
                limit_price=None,
            )
        ],
    }
    data.update(kwargs)
    return Order(**data)  # type: ignore[arg-type]


def mandate(*, crypto: bool = True) -> Mandate:
    return Mandate(
        symbol_universe=["BTC-USD", "NVDA"],
        max_position_pct_equity=50,
        max_order_notional_usd=1000,
        max_gross_exposure_pct=100,
        max_daily_loss_pct=10,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=0,
        live=MandateLive(
            crypto_stage_enabled=crypto,
            require_limit_orders=True,
            max_live_order_notional_usd=1000,
            max_account_allocation_usd=10000,
        ),
    )


def live_context(**kwargs: object) -> dict[str, object]:
    data: dict[str, object] = {
        "quote_price": Decimal("100"),
        "quote_ts": NOW,
        "now": NOW,
        "live_orders_today": 0,
        "broker_day_pnl": Decimal("0"),
        "live_positions": {},
        "reconciliation_stale": False,
        "reconciliation_age_ticks": 0,
    }
    data.update(kwargs)
    return data


@dataclass
class BrokerOrder:
    client_order_id: str
    status: str = "working"
    broker_order_id: str = "broker-1"
    venue: Venue = "robinhood_crypto"
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    order: Order | None = None


class FakeLiveBroker:
    def __init__(self) -> None:
        self.orders: list[object] = []
        self.positions_data: list[object] = []
        self.cash = Decimal("10000")
        self.cancelled: list[str] = []
        self.submitted: list[Order] = []

    async def submit(self, submitted_order: Order) -> Fill | OrderRejected | OrderPending:
        self.submitted.append(submitted_order)
        return OrderPending(client_order_id=submitted_order.client_order_id, venue="robinhood_crypto")

    async def cancel(self, client_order_id: str) -> bool:
        self.cancelled.append(client_order_id)
        return True

    async def open_orders(self) -> list[object]:
        return self.orders

    async def positions(self) -> list[object]:
        return self.positions_data

    def capabilities(self) -> set[str]:
        return {"crypto"}


class FakePaper:
    async def submit(self, submitted_order: Order) -> Fill:
        return Fill(
            order_id=submitted_order.order_id,
            price=Decimal("1"),
            qty=submitted_order.qty,
            ts=NOW,
            slippage_usd=Decimal("0"),
            commission_usd=Decimal("0"),
        )

    async def get_quote(self, symbol: str) -> object:
        return object()


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
async def test_lifecycle_partial_then_full_fill_updates_portfolio_and_events(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order()
    seed_live_order(active, active_order)
    broker = FakeLiveBroker()
    bus = EventBus()
    queue = await bus.subscribe_queue()
    reconciler = Reconciler(
        active,
        brokers={"robinhood_crypto": broker},
        bus=bus,
        clock=lambda: NOW,
    )

    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="partial",
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("10"),
            order=active_order,
        )
    ]
    await reconciler.run_once()
    portfolio = load_portfolio(active)
    assert portfolio.positions[0].qty == Decimal("2")

    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="filled",
            filled_qty=Decimal("5"),
            avg_fill_price=Decimal("11"),
            order=active_order,
        )
    ]
    await reconciler.run_once()

    portfolio = load_portfolio(active)
    assert portfolio.positions[0].qty == Decimal("5")
    assert active.execute("SELECT status FROM live_orders").fetchone()["status"] == "filled"
    events = [await asyncio.wait_for(queue.get(), timeout=1) for _ in range(4)]
    assert sum(isinstance(event, OrderFilled) for event in events) == 2
    assert any(isinstance(event, EquityUpdated) for event in events)


@pytest.mark.asyncio
async def test_incremental_fill_uses_cumulative_vwap_delta_price(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order(qty=Decimal("100"))
    seed_live_order(active, active_order)
    broker = FakeLiveBroker()
    reconciler = Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW)

    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="partial",
            filled_qty=Decimal("60"),
            avg_fill_price=Decimal("10"),
            order=active_order,
        )
    ]
    broker.positions_data = [{"symbol": "BTC-USD", "qty": "60"}]
    await reconciler.run_once()

    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="filled",
            filled_qty=Decimal("100"),
            avg_fill_price=Decimal("14"),
            order=active_order,
        )
    ]
    broker.positions_data = [{"symbol": "BTC-USD", "qty": "100"}]
    await reconciler.run_once()

    fills = get_fills(active, order_id=active_order.order_id)
    portfolio = load_portfolio(active)
    assert fills[1].price == Decimal("20")
    assert portfolio.positions[0].avg_cost == Decimal("14")
    assert portfolio.cash == Decimal("8600")


@pytest.mark.asyncio
async def test_crypto_asset_code_holdings_reconcile_against_mirror_symbol(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order(qty=Decimal("0.5"))
    seed_live_order(active, active_order)
    broker = FakeLiveBroker()
    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="filled",
            filled_qty=Decimal("0.5"),
            avg_fill_price=Decimal("100"),
            order=active_order,
        )
    ]
    broker.positions_data = [{"asset_code": "BTC", "qty": Decimal("0.5"), "venue": "robinhood_crypto"}]
    broker.cash = Decimal("9950")
    reconciler = Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW)

    result = await reconciler.run_once()

    assert result.ok
    assert result.results[0].diff == {}


@pytest.mark.asyncio
async def test_live_option_fill_price_matches_paper_total_premium(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = option_order()
    seed_live_order(active, active_order)
    broker = FakeLiveBroker()
    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="filled",
            venue="robinhood_agentic",
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("2"),
            order=active_order,
        )
    ]
    reconciler = Reconciler(active, brokers={"robinhood_agentic": broker}, clock=lambda: NOW)

    await reconciler.run_once()

    portfolio = load_portfolio(active)
    options = load_option_positions(active)
    assert portfolio.cash == Decimal("9800")
    assert options[0].open_premium == Decimal("200")


@pytest.mark.asyncio
async def test_incremental_option_fills_accumulate_single_position(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = option_order(contracts=2)
    seed_live_order(active, active_order)
    broker = FakeLiveBroker()
    reconciler = Reconciler(active, brokers={"robinhood_agentic": broker}, clock=lambda: NOW)

    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="partial",
            venue="robinhood_agentic",
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("1"),
            order=active_order,
        )
    ]
    await reconciler.run_once()
    broker.orders = [
        BrokerOrder(
            client_order_id=active_order.client_order_id,
            status="filled",
            venue="robinhood_agentic",
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("1.5"),
            order=active_order,
        )
    ]
    await reconciler.run_once()

    options = load_option_positions(active)
    assert len(options) == 1
    assert options[0].legs[0].contracts == 2
    assert options[0].open_premium == Decimal("300")


@pytest.mark.asyncio
async def test_ttl_exceeded_order_is_cancelled_and_audited(tmp_path: Path) -> None:
    active = conn(tmp_path)
    active_order = order()
    seed_live_order(active, active_order, submitted_at=NOW - timedelta(minutes=31))
    broker = FakeLiveBroker()
    reconciler = Reconciler(
        active,
        brokers={"robinhood_crypto": broker},
        live_order_ttl=timedelta(minutes=30),
        clock=lambda: NOW,
    )

    await reconciler.run_once()

    assert broker.cancelled == [active_order.client_order_id]
    assert active.execute("SELECT status FROM live_orders").fetchone()["status"] == "cancelled"
    assert any(row["kind"] == "live_order_ttl_cancelled" for row in read_audit())


@pytest.mark.asyncio
async def test_submit_timeout_crash_resume_requeries_client_order_id_without_duplicate_submit() -> None:
    active_order = order()
    broker = FakeLiveBroker()
    broker.orders = [BrokerOrder(client_order_id=active_order.client_order_id, order=active_order)]
    router = ExecutionRouter(
        FakePaper(),
        crypto=broker,
        live_mandate=LiveMandate(mandate()),
        reconciliation_context=live_context,
    )

    result = await router.submit(active_order)

    assert isinstance(result, OrderPending)
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_position_cash_mismatch_halts_live_until_clean_reconciliation(tmp_path: Path) -> None:
    active = conn(tmp_path)
    broker = FakeLiveBroker()
    broker.positions_data = [{"symbol": "BTC-USD", "qty": "1"}]
    bus = EventBus()
    queue = await bus.subscribe_queue()
    reconciler = Reconciler(active, brokers={"robinhood_crypto": broker}, bus=bus, clock=lambda: NOW)

    result = await reconciler.run_once()

    assert not result.ok
    assert active.execute("SELECT ok FROM reconciliations").fetchone()["ok"] == 0
    assert isinstance(await asyncio.wait_for(queue.get(), timeout=1), ReconciliationFailed)
    status = ReconciliationStatus(active, interval_seconds=300, clock=lambda: NOW)
    router = ExecutionRouter(
        FakePaper(),
        crypto=broker,
        live_mandate=LiveMandate(mandate()),
        reconciliation_context=lambda: live_context(**status.context()),
    )
    rejected = await router.submit(order(qty=Decimal("1")))
    assert isinstance(rejected, OrderRejected)
    assert rejected.code == "RECONCILIATION_MISMATCH"

    broker.positions_data = []
    clean = await reconciler.run_once()
    assert clean.ok
    accepted = await router.submit(order(order_id="order-2", client_order_id="client-2", qty=Decimal("1")))
    assert isinstance(accepted, OrderPending)


@pytest.mark.asyncio
async def test_crash_resume_status_is_stale_until_startup_reconcile_pass(tmp_path: Path) -> None:
    active = conn(tmp_path)
    broker = FakeLiveBroker()
    status = ReconciliationStatus(active, interval_seconds=300, clock=lambda: NOW)
    assert status.context()["reconciliation_stale"] is True

    result = await Reconciler(active, brokers={"robinhood_crypto": broker}, clock=lambda: NOW).run_once()

    assert result.ok
    assert status.context()["reconciliation_stale"] is False


@pytest.mark.asyncio
async def test_default_no_live_rail_reconciler_is_inert(tmp_path: Path) -> None:
    service = build_command_service(
        bus=EventBus(),
        conn=conn(tmp_path),
        settings=Settings(),
        mandate=mandate(crypto=False),
    )

    service.start_background_services()

    assert service._reconciler_task is None
    await service.stop_background_services()

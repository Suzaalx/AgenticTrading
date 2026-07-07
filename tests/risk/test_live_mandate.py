from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.models import Mandate, Order, Quote
from sentinel.execution.broker import OrderPending, OrderRejected
from sentinel.orchestrator.service import build_command_service
from sentinel.risk.live_mandate import LiveMandate, to_marketable_limit
from sentinel.store.db import connect

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def mandate(**live_overrides: object) -> Mandate:
    data = {
        "symbol_universe": ["AAPL", "BTC-USD"],
        "max_position_pct_equity": 10.0,
        "max_order_notional_usd": 2000.0,
        "max_gross_exposure_pct": 80.0,
        "max_daily_loss_pct": 3.0,
        "max_orders_per_day": 10,
        "allow_short": False,
        "cooldown_minutes_per_symbol": 60,
        "live": {
            "crypto_stage_enabled": True,
            "equity_stage_enabled": True,
            "options_stage_enabled": True,
            "max_live_order_notional_usd": 200.0,
            "max_live_daily_loss_usd": 100.0,
            "max_live_orders_per_day": 3,
            "max_account_allocation_usd": 500.0,
            "require_limit_orders": True,
            "quote_max_age_seconds": 60,
            **live_overrides,
        },
    }
    return Mandate.model_validate(data)


def order(**overrides: object) -> Order:
    data = {
        "order_id": "order-1",
        "run_id": "run-1",
        "symbol": "BTC-USD",
        "side": "buy",
        "qty": Decimal("1"),
        "type": "limit",
        "reason": "agent_decision",
        "created_at": NOW,
        "asset_type": "crypto",
        "venue": "robinhood_crypto",
    }
    data.update(overrides)
    return Order.model_validate(data)


def context(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "now": NOW,
        "price": Decimal("100"),
        "quote_ts": NOW,
        "broker_day_pnl": Decimal("0"),
        "live_orders_today": 0,
        "live_positions": [],
    }
    data.update(overrides)
    return data


def codes(result: object) -> set[str]:
    return {violation.code for violation in result.violations}  # type: ignore[attr-defined]


def test_live_mandate_passes_clean_order() -> None:
    result = LiveMandate(mandate()).check_live_order(order(), **context())
    assert result.passed


@pytest.mark.parametrize(
    ("test_order", "test_mandate", "kwargs", "expected"),
    [
        (order(), mandate(crypto_stage_enabled=False), {}, "LIVE_STAGE_NOT_ENABLED"),
        (order(qty=Decimal("3")), mandate(), {}, "LIVE_NOTIONAL_EXCEEDED"),
        (order(), mandate(), {"live_orders_today": 3}, "LIVE_ORDER_LIMIT"),
        (order(), mandate(), {"quote_ts": NOW - timedelta(seconds=61)}, "QUOTE_TOO_OLD"),
        (order(), mandate(), {"broker_day_pnl": Decimal("-101")}, "LIVE_DAILY_LOSS_HALT"),
        (order(), mandate(), {"reconciliation_stale": True}, "RECONCILIATION_MISMATCH"),
        (
            order(),
            mandate(),
            {"live_positions": [{"symbol": "AAPL", "notional": Decimal("450")}]},
            "LIVE_NOTIONAL_EXCEEDED",
        ),
    ],
)
def test_live_mandate_violations(
    test_order: Order,
    test_mandate: Mandate,
    kwargs: dict[str, object],
    expected: str,
) -> None:
    result = LiveMandate(test_mandate).check_live_order(test_order, **context(**kwargs))
    assert expected in codes(result)
    assert not result.passed


def test_market_orders_are_vetoed_when_limits_required() -> None:
    result = LiveMandate(mandate()).check_live_order(order(type="market"), **context())
    assert not result.passed


def test_to_marketable_limit_returns_limit_copy_and_buffered_price() -> None:
    quote = Quote(symbol="BTC-USD", price=Decimal("100"), ts=NOW, source="fake")
    limit_order, limit_price = to_marketable_limit(order(type="market"), quote, buffer_bps=10)
    assert limit_order.type == "limit"
    assert limit_price == Decimal("100.10")


class FakeLiveBroker:
    def __init__(self) -> None:
        self.submitted: list[Order] = []

    async def submit(self, submitted_order: Order) -> OrderPending:
        self.submitted.append(submitted_order)
        return OrderPending(client_order_id=submitted_order.client_order_id, venue="robinhood_crypto")

    async def cancel(self, client_order_id: str) -> bool:
        return bool(client_order_id)

    async def open_orders(self) -> list[object]:
        return []

    async def positions(self) -> list[object]:
        return []

    def capabilities(self) -> set[str]:
        return {"crypto"}


def _service_order(**overrides: object) -> Order:
    live_order = order(**overrides)
    object.__setattr__(live_order, "limit_price", Decimal("1"))
    object.__setattr__(live_order, "quote_ts", datetime.now(UTC))
    return live_order


def _seed_fresh_reconciliation(db_conn: object, *, day_pnl: Decimal = Decimal("0")) -> None:
    now = datetime.now(UTC).isoformat()
    db_conn.execute(
        "INSERT INTO reconciliations (ts, venue, ok, diff_json) VALUES (?, ?, ?, ?)",
        (now, "robinhood_crypto", 1, "{}"),
    )
    db_conn.execute(
        "INSERT INTO equity_curve (ts, equity, cash, day_pnl) VALUES (?, ?, ?, ?)",
        (now, "10000", "10000", str(day_pnl)),
    )
    db_conn.commit()


@pytest.mark.asyncio
async def test_production_context_vetoes_oversized_live_order(tmp_path: Path) -> None:
    db_conn = connect(tmp_path / "sentinel.db")
    service = build_command_service(
        bus=EventBus(),
        conn=db_conn,
        settings=Settings(),
        mandate=mandate(),
        crypto_broker=FakeLiveBroker(),
    )
    _seed_fresh_reconciliation(db_conn)

    result = await service.execution_router.submit(_service_order(qty=Decimal("100000")))

    assert isinstance(result, OrderRejected)
    assert result.code == "LIVE_NOTIONAL_EXCEEDED"


@pytest.mark.asyncio
async def test_production_context_vetoes_broker_daily_loss(tmp_path: Path) -> None:
    db_conn = connect(tmp_path / "sentinel.db")
    service = build_command_service(
        bus=EventBus(),
        conn=db_conn,
        settings=Settings(),
        mandate=mandate(),
        crypto_broker=FakeLiveBroker(),
    )
    _seed_fresh_reconciliation(db_conn, day_pnl=Decimal("-101"))

    result = await service.execution_router.submit(_service_order())

    assert isinstance(result, OrderRejected)
    assert result.code == "LIVE_DAILY_LOSS_HALT"


@pytest.mark.asyncio
async def test_production_context_vetoes_order_with_stale_order_quote_ts(tmp_path: Path) -> None:
    db_conn = connect(tmp_path / "sentinel.db")
    service = build_command_service(
        bus=EventBus(),
        conn=db_conn,
        settings=Settings(),
        mandate=mandate(),
        crypto_broker=FakeLiveBroker(),
    )
    _seed_fresh_reconciliation(db_conn)
    stale_order = _service_order()
    object.__setattr__(stale_order, "quote_ts", datetime(2000, 1, 1, tzinfo=UTC))

    result = await service.execution_router.submit(stale_order)

    assert isinstance(result, OrderRejected)
    assert result.code == "QUOTE_TOO_OLD"

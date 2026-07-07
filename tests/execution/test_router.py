from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest

from sentinel.core.models import Fill, GateResult, Order
from sentinel.execution.broker import OrderPending, OrderRejected, Venue
from sentinel.execution.router import ExecutionRouter


def _order(
    asset_type: Literal["equity", "option", "crypto"],
    *,
    venue: Venue = "paper",
) -> Order:
    symbol = "BTC-USD" if asset_type == "crypto" else "NVDA"
    return Order(
        order_id=f"order-{asset_type}",
        run_id="run-1",
        symbol=symbol,
        side="buy",
        qty=Decimal("1"),
        type="market",
        reason="agent_decision",
        created_at=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        asset_type=asset_type,
        venue=venue,
    )


def _fill(order: Order) -> Fill:
    return Fill(
        order_id=order.order_id,
        price=Decimal("100"),
        qty=order.qty,
        ts=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        slippage_usd=Decimal("0"),
        commission_usd=Decimal("0"),
        venue=order.venue,
    )


class FakePaperBroker:
    def __init__(self) -> None:
        self.submitted: list[Order] = []

    async def submit(self, order: Order) -> Fill:
        self.submitted.append(order)
        return _fill(order)

    async def get_quote(self, symbol: str) -> object:
        return object()


class FakeLiveBroker:
    def __init__(
        self,
        capabilities: set[str],
        *,
        result: Fill | OrderRejected | OrderPending | None = None,
        raises: bool = False,
    ) -> None:
        self.submitted: list[Order] = []
        self._capabilities = capabilities
        self._result = result
        self._raises = raises

    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending:
        self.submitted.append(order)
        if self._raises:
            raise RuntimeError("venue outage")
        return self._result if self._result is not None else _fill(order)

    async def cancel(self, client_order_id: str) -> bool:
        return bool(client_order_id)

    async def open_orders(self) -> list[object]:
        return []

    async def positions(self) -> list[object]:
        return []

    def capabilities(self) -> set[str]:
        return self._capabilities


@dataclass
class FakeLiveMandate:
    crypto_stage_enabled: bool = False
    equity_stage_enabled: bool = False
    options_stage_enabled: bool = False
    passed: bool = True

    def check_live_order(self, order: Order, **context: object) -> GateResult:
        assert context["venue"] == order.venue
        return GateResult(passed=self.passed)


@pytest.mark.parametrize(
    ("asset_type", "mandate", "expected_venue"),
    [
        ("crypto", FakeLiveMandate(), "paper"),
        ("crypto", FakeLiveMandate(crypto_stage_enabled=True), "robinhood_crypto"),
        ("equity", FakeLiveMandate(), "paper"),
        ("equity", FakeLiveMandate(equity_stage_enabled=True), "robinhood_agentic"),
        ("option", FakeLiveMandate(), "paper"),
        ("option", FakeLiveMandate(options_stage_enabled=True), "robinhood_agentic"),
    ],
)
async def test_routing_matrix_asset_stage_to_venue(
    asset_type: Literal["equity", "option", "crypto"],
    mandate: FakeLiveMandate,
    expected_venue: Venue,
) -> None:
    paper = FakePaperBroker()
    crypto = FakeLiveBroker({"crypto"})
    agentic = FakeLiveBroker({"equity", "option"})
    router = ExecutionRouter(paper, crypto=crypto, agentic=agentic, live_mandate=mandate)

    result = await router.submit(_order(asset_type))

    assert not isinstance(result, OrderRejected)
    assert result.venue == expected_venue
    assert len(paper.submitted) == (1 if expected_venue == "paper" else 0)
    assert len(crypto.submitted) == (1 if expected_venue == "robinhood_crypto" else 0)
    assert len(agentic.submitted) == (1 if expected_venue == "robinhood_agentic" else 0)


@pytest.mark.parametrize(
    "live_broker",
    [
        FakeLiveBroker({"crypto"}, result=OrderRejected(reason="broker rejected")),
        FakeLiveBroker({"crypto"}, raises=True),
    ],
)
async def test_live_failure_never_falls_through_to_paper(live_broker: FakeLiveBroker) -> None:
    paper = FakePaperBroker()
    router = ExecutionRouter(
        paper,
        crypto=live_broker,
        live_mandate=FakeLiveMandate(crypto_stage_enabled=True),
    )

    result = await router.submit(_order("crypto"))

    assert isinstance(result, OrderRejected)
    assert paper.submitted == []
    assert len(live_broker.submitted) == 1


async def test_capability_rejection_for_dormant_options_stage() -> None:
    paper = FakePaperBroker()
    agentic = FakeLiveBroker({"equity"})
    router = ExecutionRouter(
        paper,
        agentic=agentic,
        live_mandate=FakeLiveMandate(options_stage_enabled=True),
    )

    result = await router.submit(_order("option"))

    assert isinstance(result, OrderRejected)
    assert result.code == "VENUE_CAPABILITY_MISSING"
    assert paper.submitted == []
    assert agentic.submitted == []


async def test_order_pending_propagates_unchanged() -> None:
    pending = OrderPending(
        client_order_id="client-1",
        venue="robinhood_crypto",
        broker_order_id="broker-1",
    )
    crypto = FakeLiveBroker({"crypto"}, result=pending)
    router = ExecutionRouter(
        FakePaperBroker(),
        crypto=crypto,
        live_mandate=FakeLiveMandate(crypto_stage_enabled=True),
    )

    result = await router.submit(_order("crypto"))

    assert result is pending


async def test_missing_live_mandate_rejects_explicit_live_route() -> None:
    paper = FakePaperBroker()
    crypto = FakeLiveBroker({"crypto"})
    router = ExecutionRouter(paper, crypto=crypto, live_mandate=None)

    result = await router.submit(_order("crypto"))

    assert isinstance(result, OrderRejected)
    assert result.code == "LIVE_STAGE_NOT_ENABLED"
    assert paper.submitted == []
    assert crypto.submitted == []


async def test_passing_live_mandate_proceeds_to_live_broker() -> None:
    paper = FakePaperBroker()
    crypto = FakeLiveBroker({"crypto"})
    router = ExecutionRouter(
        paper,
        crypto=crypto,
        live_mandate=FakeLiveMandate(crypto_stage_enabled=True),
    )

    result = await router.submit(_order("crypto"))

    assert not isinstance(result, OrderRejected)
    assert result.venue == "robinhood_crypto"
    assert paper.submitted == []
    assert len(crypto.submitted) == 1

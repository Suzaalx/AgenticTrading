from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sentinel.config.settings import RobinhoodAgenticSettings
from sentinel.core.models import OptionContract, OptionLeg, Order
from sentinel.execution.broker import OrderPending, OrderRejected
from sentinel.execution.robinhood_agentic import RobinhoodAgenticBroker

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


class FakeMcpSession:
    def __init__(self, *, option_tools: bool = False, manual_approval: bool = False) -> None:
        self.option_tools = option_tools
        self.manual_approval = manual_approval
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.orders: dict[str, dict[str, object]] = {}

    async def list_tools(self) -> dict[str, object]:
        tools = [
            {"name": "rh_equity_trade_preview"},
            {"name": "rh_equity_order_submit"},
            {"name": "rh_cancel_order"},
            {"name": "rh_list_orders"},
            {"name": "rh_positions"},
        ]
        if self.option_tools:
            tools.extend([{"name": "rh_option_trade_preview"}, {"name": "rh_option_order_submit"}])
        return {"tools": tools}

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, arguments))
        client_order_id = str(arguments.get("client_order_id", "client-1"))
        if "preview" in name:
            return {"preview_id": f"preview-{client_order_id}", "estimated_price": "100", "estimated_cost": "100"}
        if "submit" in name:
            status = "pending_approval" if self.manual_approval else "filled"
            order = {
                "broker_order_id": f"broker-{client_order_id}",
                "client_order_id": client_order_id,
                "status": status,
                "price": "100",
                "quantity": arguments.get("quantity", "1"),
            }
            self.orders[client_order_id] = order
            return order
        if "cancel" in name:
            return {"ok": True}
        if "orders" in name:
            return {"orders": list(self.orders.values())}
        if "positions" in name:
            return {"positions": [{"symbol": "NVDA", "quantity": "1"}]}
        return {}


def _audit_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    def audit(kind: str, payload: object, actor: str) -> dict[str, object]:
        row = {"kind": kind, "payload": payload, "actor": actor}
        records.append(row)
        return row

    return records, audit  # type: ignore[return-value]


def _equity_order(client_order_id: str = "client-1") -> Order:
    return Order(
        order_id="order-equity",
        run_id="run-1",
        symbol="NVDA",
        side="buy",
        qty=Decimal("1"),
        type="market",
        reason="agent_decision",
        created_at=NOW,
        asset_type="equity",
        client_order_id=client_order_id,
    )


def _option_order() -> Order:
    leg = OptionLeg(
        contract=OptionContract(
            contract_symbol="NVDA260717C00100000",
            underlying="NVDA",
            kind="call",
            strike=Decimal("100"),
            expiry=date(2026, 7, 17),
        ),
        side="buy",
        contracts=1,
        limit_price=Decimal("1.23"),
    )
    return Order(
        order_id="order-option",
        run_id="run-1",
        symbol="NVDA",
        side="buy",
        qty=Decimal("1"),
        type="net_debit",
        reason="agent_decision",
        created_at=NOW,
        asset_type="option",
        strategy="long_call",
        legs=[leg],
    )


async def test_probe_equity_only_rejects_options_and_preview_submit_fill() -> None:
    records, audit = _audit_records()
    session = FakeMcpSession()
    broker = RobinhoodAgenticBroker(
        RobinhoodAgenticSettings(enabled=True),
        session=session,
        audit=audit,
        clock=lambda: NOW,
    )

    assert await broker.probe() == {"equity"}
    assert broker.capabilities() == {"equity"}
    option_result = await broker.submit(_option_order())
    equity_result = await broker.submit(_equity_order())

    assert isinstance(option_result, OrderRejected)
    assert option_result.code == "VENUE_CAPABILITY_MISSING"
    assert not isinstance(equity_result, OrderPending | OrderRejected)
    assert equity_result.venue == "robinhood_agentic"
    assert records[0]["kind"] == "robinhood_agentic_trade_preview"
    preview_payload = records[0]["payload"]
    assert isinstance(preview_payload, dict)
    assert preview_payload["preview"] == {
        "preview_id": "preview-client-1",
        "estimated_price": "100",
        "estimated_cost": "100",
    }


async def test_manual_approval_pending_open_orders_positions_and_cancel() -> None:
    session = FakeMcpSession(manual_approval=True)
    broker = RobinhoodAgenticBroker(RobinhoodAgenticSettings(enabled=True), session=session)

    result = await broker.submit(_equity_order())
    open_orders = await broker.open_orders()
    positions = await broker.positions()
    cancelled = await broker.cancel("client-1")

    assert isinstance(result, OrderPending)
    assert open_orders[0]["status"] == "pending_approval"
    assert positions == [{"symbol": "NVDA", "quantity": "1", "venue": "robinhood_agentic"}]
    assert cancelled is True


async def test_option_tools_probe_enables_multi_leg_mapping() -> None:
    session = FakeMcpSession(option_tools=True)
    broker = RobinhoodAgenticBroker(RobinhoodAgenticSettings(enabled=True), session=session)

    assert await broker.probe() == {"equity", "option"}
    result = await broker.submit(_option_order())

    assert not isinstance(result, OrderPending | OrderRejected)
    preview_call = next(call for call in session.calls if call[0] == "rh_option_trade_preview")
    assert preview_call[1]["strategy"] == "long_call"
    assert preview_call[1]["legs"] == [
        {
            "contract_symbol": "NVDA260717C00100000",
            "underlying": "NVDA",
            "option_type": "call",
            "strike": "100",
            "expiry": "2026-07-17",
            "side": "buy",
            "quantity": 1,
            "limit_price": "1.23",
        }
    ]


async def test_submit_reuses_filled_client_order_id_without_duplicate_submit() -> None:
    session = FakeMcpSession()
    broker = RobinhoodAgenticBroker(RobinhoodAgenticSettings(enabled=True), session=session, clock=lambda: NOW)
    active_order = _equity_order("client-filled")

    first = await broker.submit(active_order)
    second = await broker.submit(active_order)

    assert not isinstance(first, OrderPending | OrderRejected)
    assert not isinstance(second, OrderPending | OrderRejected)
    assert second.broker_order_id == "broker-client-filled"
    submit_calls = [call for call in session.calls if call[0] == "rh_equity_order_submit"]
    assert len(submit_calls) == 1


async def test_option_fill_price_converts_per_contract_to_total_premium() -> None:
    session = FakeMcpSession(option_tools=True)
    broker = RobinhoodAgenticBroker(RobinhoodAgenticSettings(enabled=True), session=session, clock=lambda: NOW)

    result = await broker.submit(_option_order())

    assert not isinstance(result, OrderPending | OrderRejected)
    assert result.price == Decimal("10000")

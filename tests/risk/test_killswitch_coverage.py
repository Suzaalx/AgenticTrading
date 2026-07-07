from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from typer.testing import CliRunner

import sentinel.risk.killswitch as killswitch
from sentinel.__main__ import app
from sentinel.core.bus import EventBus
from sentinel.core.events import KillEngaged, KillSwitchChanged
from sentinel.core.models import Order
from sentinel.execution.broker import OrderPending
from sentinel.risk._events import set_event_bus
from sentinel.risk.killswitch import (
    FLATTEN_CONFIRMATION,
    disengage,
    engage,
    engage_async,
    is_engaged,
)


class RecordingBroker:
    def __init__(
        self,
        *,
        open_orders: list[object] | Exception | None = None,
        positions: list[object] | Exception | None = None,
        cancel_error: Exception | None = None,
        submit_error: Exception | None = None,
    ) -> None:
        self._open_orders = [] if open_orders is None else open_orders
        self._positions = [] if positions is None else positions
        self.cancel_error = cancel_error
        self.submit_error = submit_error
        self.cancelled: list[str] = []
        self.submitted: list[Order] = []

    async def submit(self, order: Order) -> OrderPending:
        self.submitted.append(order)
        if self.submit_error is not None:
            raise self.submit_error
        return OrderPending(client_order_id=order.client_order_id, venue=order.venue)

    async def cancel(self, client_order_id: str) -> bool:
        self.cancelled.append(client_order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return client_order_id != "reject-me"

    async def open_orders(self) -> list[object]:
        if isinstance(self._open_orders, Exception):
            raise self._open_orders
        return self._open_orders

    async def positions(self) -> list[object]:
        if isinstance(self._positions, Exception):
            raise self._positions
        return self._positions

    def capabilities(self) -> set[str]:
        return {"crypto", "equity"}


class Router:
    def __init__(self, brokers: dict[str, RecordingBroker]) -> None:
        self._brokers = brokers

    def live_brokers(self) -> dict[str, RecordingBroker]:
        return self._brokers


@pytest.mark.asyncio
async def test_engage_async_cancels_flattens_and_emits_kill_engaged() -> None:
    bus = EventBus()
    queue = await bus.subscribe_queue()
    broker = RecordingBroker(
        open_orders=[
            {"client_order_id": "cancel-me"},
            {"id": "reject-me"},
            {"symbol": "missing-id"},
        ],
        positions=[
            {"symbol": "BTC-USD", "quantity": "0.25"},
            {"symbol": "AAPL", "qty": Decimal("-2")},
            {"symbol": "EMPTY", "qty": Decimal("0")},
        ],
    )
    set_event_bus(bus)
    try:
        await engage_async(
            actor="tester",
            brokers={"robinhood_crypto": broker},
            flatten=True,
            flatten_confirmation=FLATTEN_CONFIRMATION,
        )
    finally:
        set_event_bus(None)

    assert is_engaged()
    assert broker.cancelled == ["cancel-me", "reject-me"]
    assert [order.side for order in broker.submitted] == ["sell", "buy"]
    assert all(order.type == "market" for order in broker.submitted)
    await asyncio.sleep(0)
    events = [queue.get_nowait(), queue.get_nowait()]
    assert any(isinstance(event, KillSwitchChanged) and event.enabled for event in events)
    kill_event = next(event for event in events if isinstance(event, KillEngaged))
    venue_results = kill_event.cancel_results["robinhood_crypto"]
    assert venue_results[1] == {"client_order_id": "reject-me", "ok": False}
    assert venue_results[2]["error"] == "open order missing client_order_id"
    assert kill_event.flatten_results["robinhood_crypto"][2]["error"] == (
        "position missing symbol or quantity"
    )


@pytest.mark.parametrize("async_entrypoint", [False, True])
def test_flatten_requires_exact_confirmation(async_entrypoint: bool) -> None:
    if async_entrypoint:
        with pytest.raises(ValueError, match="flatten requires confirmation"):
            asyncio.run(engage_async(flatten=True, flatten_confirmation="wrong"))
    else:
        with pytest.raises(ValueError, match="flatten requires confirmation"):
            engage(flatten=True, flatten_confirmation="wrong")


def test_engage_with_router_records_cancel_and_flatten_errors() -> None:
    broker = RecordingBroker(
        open_orders=RuntimeError("orders offline"),
        positions=RuntimeError("positions offline"),
    )
    bus = EventBus()
    queue = asyncio.run(bus.subscribe_queue())
    set_event_bus(bus)
    try:
        engage(
            actor="router-test",
            router=Router({"robinhood_crypto": broker}),
            flatten=True,
            flatten_confirmation=FLATTEN_CONFIRMATION,
        )
    finally:
        set_event_bus(None)

    # Synchronous engagement publishes immediately through the queue created above.
    kill_event = next(
        event
        for event in [queue.get_nowait(), queue.get_nowait()]
        if isinstance(event, KillEngaged)
    )
    assert kill_event.cancel_results["robinhood_crypto"] == [
        {"ok": False, "error": "orders offline"}
    ]
    assert kill_event.flatten_results["robinhood_crypto"] == [
        {"ok": False, "error": "positions offline"}
    ]


@pytest.mark.asyncio
async def test_sync_engage_with_live_brokers_is_guarded_inside_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = RecordingBroker(open_orders=[])
    monkeypatch.setattr(killswitch, "_cancel_and_maybe_flatten", lambda brokers, flatten: object())

    with pytest.raises(RuntimeError, match="active event loop"):
        engage(actor="loop", brokers={"robinhood_crypto": broker})


@pytest.mark.asyncio
async def test_engage_async_records_cancel_and_submit_exceptions() -> None:
    broker = RecordingBroker(
        open_orders=[{"client_order_id": "boom"}],
        positions=[{"symbol": "AAPL", "qty": 1}],
        cancel_error=RuntimeError("cancel failed"),
        submit_error=RuntimeError("submit failed"),
    )
    bus = EventBus()
    queue = await bus.subscribe_queue()
    set_event_bus(bus)
    try:
        await engage_async(
            actor="errors",
            brokers={"robinhood_agentic": broker},
            flatten=True,
            flatten_confirmation=FLATTEN_CONFIRMATION,
        )
    finally:
        set_event_bus(None)

    await asyncio.sleep(0)
    kill_event = next(
        event
        for event in [queue.get_nowait(), queue.get_nowait()]
        if isinstance(event, KillEngaged)
    )
    assert kill_event.cancel_results["robinhood_agentic"] == [
        {"client_order_id": "boom", "ok": False, "error": "cancel failed"}
    ]
    assert kill_event.flatten_results["robinhood_agentic"] == [
        {"symbol": "AAPL", "ok": False, "error": "submit failed"}
    ]


def test_disengage_emits_disabled_event_even_when_file_is_absent() -> None:
    bus = EventBus()
    queue = asyncio.run(bus.subscribe_queue())
    set_event_bus(bus)
    try:
        disengage("tester")
    finally:
        set_event_bus(None)

    event = queue.get_nowait()
    assert isinstance(event, KillSwitchChanged)
    assert not event.enabled


def test_graduate_is_blocked_while_kill_switch_engaged() -> None:
    engage("tester")

    result = CliRunner().invoke(app, ["graduate", "crypto", "--root", "."])

    assert result.exit_code == 1
    assert "kill switch is engaged" in result.output

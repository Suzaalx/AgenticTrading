from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sentinel.__main__ as sentinel_cli
from sentinel.__main__ import app
from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.events import KillEngaged
from sentinel.core.models import Fill, Mandate, Order
from sentinel.execution.broker import OrderPending, OrderRejected
from sentinel.orchestrator.service import build_command_service
from sentinel.risk._events import set_event_bus
from sentinel.risk.killswitch import engage
from sentinel.store.db import connect


class FakeBroker:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending:
        return OrderPending(client_order_id=order.client_order_id, venue="robinhood_crypto")

    async def cancel(self, client_order_id: str) -> bool:
        self.cancelled.append(client_order_id)
        return True

    async def open_orders(self) -> list[object]:
        return [{"client_order_id": "live-1"}, {"client_order_id": "live-2"}]

    async def positions(self) -> list[object]:
        return []

    def capabilities(self) -> set[str]:
        return {"crypto"}


def test_engage_cancels_open_live_orders_and_emits_kill_engaged() -> None:
    bus = EventBus()
    queue = asyncio.run(bus.subscribe_queue())
    set_event_bus(bus)
    broker = FakeBroker()
    try:
        engage(actor="tester", brokers={"robinhood_crypto": broker})
    finally:
        set_event_bus(None)

    assert broker.cancelled == ["live-1", "live-2"]
    events = [queue.get_nowait(), queue.get_nowait()]
    kill_event = next(event for event in events if isinstance(event, KillEngaged))
    assert kill_event.cancel_results["robinhood_crypto"][0]["client_order_id"] == "live-1"


def live_mandate() -> Mandate:
    return Mandate.model_validate(
        {
            "symbol_universe": ["BTC-USD"],
            "max_position_pct_equity": 10.0,
            "max_order_notional_usd": 2000.0,
            "max_gross_exposure_pct": 80.0,
            "max_daily_loss_pct": 3.0,
            "max_orders_per_day": 10,
            "allow_short": False,
            "cooldown_minutes_per_symbol": 60,
            "live": {"crypto_stage_enabled": True},
        }
    )


async def _toggle_service_kill(tmp_path: Path) -> tuple[FakeBroker, list[object]]:
    bus = EventBus()
    queue = await bus.subscribe_queue()
    broker = FakeBroker()
    service = build_command_service(
        bus=bus,
        conn=connect(tmp_path / "sentinel.db"),
        settings=Settings(),
        mandate=live_mandate(),
        crypto_broker=broker,
    )

    await service.toggle_kill(True)
    await asyncio.sleep(0)

    events: list[object] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return broker, events


def test_service_toggle_kill_cancels_enabled_live_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_HOME", str(tmp_path / "home"))

    broker, events = asyncio.run(_toggle_service_kill(tmp_path))

    assert broker.cancelled == ["live-1", "live-2"]
    assert any(isinstance(event, KillEngaged) for event in events)


def test_cli_kill_cancels_enabled_live_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = FakeBroker()
    monkeypatch.setenv("SENTINEL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        sentinel_cli,
        "_live_brokers_from_settings",
        lambda settings, mandate: {"robinhood_crypto": broker},
    )

    result = CliRunner().invoke(app, ["kill", "on"])

    assert result.exit_code == 0
    assert broker.cancelled == ["live-1", "live-2"]

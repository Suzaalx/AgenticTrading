from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sentinel.config.settings import RobinhoodSettings
from sentinel.core.models import Order
from sentinel.execution.broker import OrderPending, OrderRejected
from sentinel.execution.robinhood_crypto import RobinhoodCryptoBroker

PEM = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB
-----END PRIVATE KEY-----"""
NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class FakeHttpClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.orders: dict[str, dict[str, object]] = {}
        self.fail_next_submit = False
        self.create_on_failed_submit = False

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if "best_bid_ask" in url:
            return FakeResponse(200, {"results": [{"symbol": "BTC-USD", "bid_price": "99", "ask_price": "101"}]})
        if "holdings" in url:
            return FakeResponse(200, {"results": [{"asset": "BTC", "quantity": "0.5"}]})
        if method == "GET" and "client_order_id=" in url:
            client_order_id = url.rsplit("client_order_id=", 1)[1]
            order = self.orders.get(client_order_id)
            return FakeResponse(200, {"results": [] if order is None else [order]})
        if method == "GET" and "orders" in url:
            return FakeResponse(200, {"results": list(self.orders.values())})
        if method == "POST" and url.endswith("/cancel/"):
            return FakeResponse(200, {"ok": True})
        if method == "POST" and url.endswith("/orders/"):
            body = _body(kwargs)
            client_order_id = str(body["client_order_id"])
            if self.fail_next_submit:
                self.fail_next_submit = False
                if self.create_on_failed_submit:
                    self.orders[client_order_id] = _broker_order(client_order_id)
                return FakeResponse(503, {"error": "busy"})
            self.orders[client_order_id] = _broker_order(client_order_id)
            return FakeResponse(200, self.orders[client_order_id])
        return FakeResponse(404, {"error": "not found"})


def _body(kwargs: dict[str, object]) -> dict[str, Any]:
    import json

    content = kwargs["content"]
    assert isinstance(content, bytes)
    return json.loads(content.decode())


def _broker_order(client_order_id: str, *, status: str = "open") -> dict[str, object]:
    return {
        "id": f"broker-{client_order_id}",
        "client_order_id": client_order_id,
        "status": status,
        "filled_quantity": "0",
        "average_price": "100",
    }


def _order(symbol: str = "BTC-USD", *, client_order_id: str = "client-1") -> Order:
    return Order(
        order_id=f"order-{symbol}",
        run_id="run-1",
        symbol=symbol,
        side="buy",
        qty=Decimal("0.01"),
        type="market",
        reason="agent_decision",
        created_at=NOW,
        asset_type="crypto",
        client_order_id=client_order_id,
    )


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBINHOOD_API_KEY", "rh-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", PEM)


def _broker(client: FakeHttpClient) -> RobinhoodCryptoBroker:
    return RobinhoodCryptoBroker(
        RobinhoodSettings(enabled=True),
        client=client,
        clock=lambda: NOW,
        sleep=lambda _: _noop(),
    )


async def _noop() -> None:
    return None


async def test_crypto_lifecycle_submit_status_cancel_positions_and_capabilities() -> None:
    client = FakeHttpClient()
    broker = _broker(client)

    result = await broker.submit(_order())
    open_orders = await broker.open_orders()
    positions = await broker.positions()
    cancelled = await broker.cancel("client-1")

    assert isinstance(result, OrderPending)
    assert result.client_order_id == "client-1"
    assert open_orders[0]["client_order_id"] == "client-1"
    assert positions == [
        {
            "asset": "BTC",
            "quantity": "0.5",
            "symbol": "BTC-USD",
            "qty": Decimal("0.5"),
            "venue": "robinhood_crypto",
        }
    ]
    assert cancelled is True
    assert broker.capabilities() == {"crypto"}
    submit_calls = [call for call in client.requests if call["method"] == "POST" and str(call["url"]).endswith("/orders/")]
    assert len(submit_calls) == 1


async def test_crypto_submit_returns_fill_for_immediate_fill() -> None:
    client = FakeHttpClient()
    client.orders["client-filled"] = _broker_order("client-filled", status="filled")
    broker = _broker(client)

    result = await broker.submit(_order(client_order_id="client-filled"))

    assert not isinstance(result, OrderPending | OrderRejected)
    assert result.venue == "robinhood_crypto"
    assert result.broker_order_id == "broker-client-filled"


async def test_disallowed_symbol_rejected_before_network() -> None:
    client = FakeHttpClient()
    broker = _broker(client)

    result = await broker.submit(_order("DOGE-USD"))

    assert isinstance(result, OrderRejected)
    assert result.code == "VENUE_CAPABILITY_MISSING"
    assert client.requests == []


async def test_transient_submit_requeries_client_order_id_without_duplicate_submit() -> None:
    client = FakeHttpClient()
    client.fail_next_submit = True
    client.create_on_failed_submit = True
    broker = _broker(client)

    result = await broker.submit(_order())

    assert isinstance(result, OrderPending)
    submit_calls = [call for call in client.requests if call["method"] == "POST" and str(call["url"]).endswith("/orders/")]
    assert len(submit_calls) == 1

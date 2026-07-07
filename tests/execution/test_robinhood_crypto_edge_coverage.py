
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.config.settings import RobinhoodSettings
from sentinel.core.models import Order
from sentinel.execution.broker import OrderPending, OrderRejected
from sentinel.execution.robinhood_crypto import (
    RobinhoodCryptoBroker,
    _decode_response,
    _ed25519_sign,
    _ed25519_sign_seed,
    _seed_from_pkcs8_pem,
)

PEM = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB
-----END PRIVATE KEY-----"""
NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code: int, payload: object, *, text_raises: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._text_raises = text_raises

    @property
    def text(self) -> str:
        if self._text_raises:
            raise RuntimeError("no text")
        return str(self._payload)

    def json(self) -> object:
        return self._payload


class RecordingClient:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = responses or []
        self.requests: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        if "client_order_id=" in url:
            return FakeResponse(200, {"results": []})
        if "best_bid_ask" in url:
            return FakeResponse(200, {"results": [{"bid": "99", "ask": "101"}]})
        if method == "POST" and url.endswith("/orders/"):
            body = _json_body(kwargs)
            return FakeResponse(
                200,
                {
                    "id": "broker-new",
                    "client_order_id": body["client_order_id"],
                    "status": "open",
                    "filled_quantity": "0",
                },
            )
        return FakeResponse(200, {})


def _json_body(kwargs: dict[str, object]) -> dict[str, Any]:
    content = kwargs["content"]
    assert isinstance(content, bytes)
    return json.loads(content.decode())


def order(**kwargs: object) -> Order:
    data = {
        "order_id": "order-1",
        "run_id": "run-1",
        "symbol": "BTC-USD",
        "side": "buy",
        "qty": Decimal("0.01"),
        "type": "market",
        "reason": "agent_decision",
        "created_at": NOW,
        "asset_type": "crypto",
        "client_order_id": "client-1",
    }
    data.update(kwargs)
    return Order(**data)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBINHOOD_API_KEY", "rh-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", PEM)


def broker(
    client: object,
    *,
    max_retries: int = 0,
    sleep_calls: list[float] | None = None,
    clock=lambda: NOW,
) -> RobinhoodCryptoBroker:
    async def sleep(seconds: float) -> None:
        if sleep_calls is not None:
            sleep_calls.append(seconds)

    return RobinhoodCryptoBroker(
        RobinhoodSettings(enabled=True),
        client=client,
        clock=clock,
        sleep=sleep,
        max_retries=max_retries,
    )


def test_constructor_rejects_disabled_and_non_crypto_only() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        RobinhoodCryptoBroker(RobinhoodSettings(enabled=False), client=RecordingClient())
    with pytest.raises(ValueError, match="crypto-only"):
        RobinhoodCryptoBroker(
            RobinhoodSettings(enabled=True, crypto_only=False),
            client=RecordingClient(),
        )


async def test_submit_classifies_validation_auth_and_unsupported_orders() -> None:
    validation = await broker(
        RecordingClient(
            [
                FakeResponse(200, {"results": []}),
                FakeResponse(200, {"results": [{"price": "100"}]}),
                FakeResponse(422, {"error": "bad qty"}),
            ]
        )
    ).submit(order())
    assert isinstance(validation, OrderRejected)
    assert "bad qty" in validation.reason

    auth = await broker(
        RecordingClient(
            [
                FakeResponse(200, {"results": []}),
                FakeResponse(200, {"results": [{"price": "100"}]}),
                FakeResponse(401, {"error": "expired"}),
            ]
        )
    ).submit(order(client_order_id="client-auth"))
    assert isinstance(auth, OrderRejected)
    assert auth.reason.startswith("Robinhood crypto auth failed")

    equity = await broker(RecordingClient()).submit(order(asset_type="equity"))
    unsupported_type = await broker(RecordingClient()).submit(order(type="net_debit"))
    assert isinstance(equity, OrderRejected)
    assert equity.code == "VENUE_CAPABILITY_MISSING"
    assert isinstance(unsupported_type, OrderRejected)
    assert "unsupported" in unsupported_type.reason


async def test_transient_retry_is_bounded_and_requeries_before_resubmit() -> None:
    sleeps: list[float] = []
    client = RecordingClient(
        [
            FakeResponse(200, {"results": []}),
            FakeResponse(200, {"results": [{"price": "100"}]}),
            FakeResponse(429, {"error": "rate"}),
            FakeResponse(200, {"results": []}),
            FakeResponse(503, {"error": "busy"}, text_raises=True),
            FakeResponse(200, {"results": []}),
        ]
    )

    result = await broker(client, max_retries=1, sleep_calls=sleeps).submit(order())

    assert isinstance(result, OrderRejected)
    assert "transient Robinhood crypto error" in result.reason
    submit_calls = [call for call in client.requests if call["method"] == "POST"]
    lookup_calls = [call for call in client.requests if "client_order_id=" in str(call["url"])]
    assert len(submit_calls) == 2
    assert len(lookup_calls) == 3
    assert sleeps == [0.25]


async def test_market_order_becomes_marketable_limit_for_buy_and_sell() -> None:
    buy_client = RecordingClient()
    sell_client = RecordingClient()

    buy_result = await broker(buy_client).submit(order(side="buy"))
    sell_result = await broker(sell_client).submit(
        order(order_id="order-sell", client_order_id="client-sell", side="sell")
    )

    assert isinstance(buy_result, OrderPending)
    assert isinstance(sell_result, OrderPending)
    buy_body = _json_body([c for c in buy_client.requests if c["method"] == "POST"][-1])
    sell_body = _json_body([c for c in sell_client.requests if c["method"] == "POST"][-1])
    assert buy_body["type"] == "limit"
    assert buy_body["limit_price"] == "100.10"
    assert sell_body["limit_price"] == "99.90"


async def test_quote_cancel_orders_holdings_and_request_edge_branches() -> None:
    client = RecordingClient(
        [
            FakeResponse(200, {"results": [{"symbol": "ETH-USD", "bid_price": "2000", "ask_price": "2010"}]}),
            FakeResponse(200, {"results": [{"symbol": "BTC-USD"}]}),
            FakeResponse(200, {"results": []}),
            FakeResponse(200, {"results": [{"client_order_id": "client-no-id"}]}),
            FakeResponse(200, {"results": [{"id": "broker-1", "client_order_id": "client-cancel"}]}),
            FakeResponse(500, {"error": "cannot cancel"}),
            FakeResponse(
                200,
                {
                    "results": [
                        {"id": "open", "client_order_id": "c1", "status": "queued", "filled_qty": "1"},
                        {"id": "done", "client_order_id": "c2", "status": "filled"},
                    ]
                },
            ),
            FakeResponse(200, {"data": [{"asset_code": "eth", "total_quantity": "2.5"}]}),
        ]
    )
    active = broker(client)

    quote = await active.get_quote("ETH-USD")
    with pytest.raises(LookupError):
        await active.get_quote("BTC-USD")
    assert await active.cancel("missing") is False
    assert await active.cancel("client-no-id") is False
    assert await active.cancel("client-cancel") is False
    assert await active.open_orders() == [
        {
            "id": "open",
            "client_order_id": "c1",
            "status": "queued",
            "filled_qty": "1",
            "broker_order_id": "open",
            "venue": "robinhood_crypto",
            "avg_fill_price": "0",
        }
    ]
    assert await active.positions() == [
        {
            "asset_code": "eth",
            "total_quantity": "2.5",
            "symbol": "ETH-USD",
            "qty": Decimal("2.5"),
            "venue": "robinhood_crypto",
        }
    ]
    assert quote.price == Decimal("2005")

    no_request = object()
    with pytest.raises(RuntimeError, match="request"):
        await broker(no_request)._request("GET", "/anything")


async def test_throttle_waits_and_signing_requires_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    times = [NOW, NOW + timedelta(seconds=1)]

    def clock() -> datetime:
        return times.pop(0) if times else NOW + timedelta(seconds=1)

    active = RobinhoodCryptoBroker(
        RobinhoodSettings(enabled=True),
        client=RecordingClient([FakeResponse(200, {"ok": True}), FakeResponse(200, {"ok": True})]),
        clock=clock,
        sleep=lambda seconds: _record_sleep(calls, seconds),
        min_interval_seconds=5,
    )

    await active._request("GET", "/first")
    await active._request("GET", "/second")
    assert calls == [5.0]

    monkeypatch.delenv("ROBINHOOD_API_KEY")
    with pytest.raises(RuntimeError, match="API key"):
        active._sign_headers("GET", "/path")


async def _record_sleep(calls: list[float], seconds: float) -> None:
    calls.append(seconds)



def test_pure_python_ed25519_signing_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    signature = _ed25519_sign_seed(bytes(range(32)), b"message")
    assert len(signature) == 64
    assert signature == _ed25519_sign_seed(bytes(range(32)), b"message")

    with pytest.raises(RuntimeError, match="PKCS8"):
        _seed_from_pkcs8_pem("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----")

    def missing_crypto(name: str) -> object:
        if name == "cryptography.hazmat.primitives.serialization":
            raise ModuleNotFoundError(name)
        raise AssertionError(name)

    monkeypatch.setattr("sentinel.execution.robinhood_crypto.importlib.import_module", missing_crypto)
    assert _ed25519_sign(PEM, b"fallback") == _ed25519_sign_seed(bytes([1]) * 32, b"fallback")


async def test_decode_response_accepts_mapping_and_empty_objects() -> None:
    assert await _decode_response({"ok": True}) == {"ok": True}
    assert await _decode_response(object()) == {}
    assert await _decode_response(FakeResponse(200, [1, 2])) == {"results": [1, 2]}

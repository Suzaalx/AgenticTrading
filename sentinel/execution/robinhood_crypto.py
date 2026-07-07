"""Robinhood Crypto Trading API live broker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlencode

from sentinel.config.settings import RobinhoodSettings
from sentinel.core.models import Fill, Order, Quote, ViolationCode
from sentinel.execution.broker import LiveBroker, OrderPending, OrderRejected

JsonObject = dict[str, object]
Sleep = Callable[[float], Awaitable[object]]

_ALLOWED_CRYPTO_SYMBOLS = {"BTC-USD", "ETH-USD"}
_TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_TERMINAL_FILLED = {"filled", "executed", "complete", "completed"}
_WORKING = {"open", "queued", "new", "partially_filled", "pending", "working"}


class RobinhoodCryptoBroker(LiveBroker):
    """Official Robinhood Crypto Trading API connector for BTC-USD and ETH-USD."""

    def __init__(
        self,
        settings: RobinhoodSettings | None = None,
        *,
        client: object | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Sleep | None = None,
        max_retries: int = 2,
        min_interval_seconds: float = 0.0,
    ) -> None:
        self.settings = settings or RobinhoodSettings()
        if not self.settings.enabled:
            raise RuntimeError("RobinhoodCryptoBroker is disabled")
        if not self.settings.crypto_only:
            raise ValueError("RobinhoodCryptoBroker only supports crypto-only mode")
        self._allowed_symbols = set(self.settings.allowed_symbols) & _ALLOWED_CRYPTO_SYMBOLS
        self._client = client if client is not None else _build_httpx_client()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._max_retries = max(max_retries, 0)
        self._min_interval_seconds = max(min_interval_seconds, 0.0)
        self._last_request_at = 0.0

    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending:
        """Submit an idempotent market/limit crypto order."""

        rejection = self._symbol_rejection(order.symbol)
        if rejection is not None:
            return rejection
        if order.asset_type != "crypto":
            return _reject("VENUE_CAPABILITY_MISSING", "Robinhood crypto only supports crypto orders")
        if order.type not in {"market", "limit"}:
            return OrderRejected(reason=f"unsupported robinhood crypto order type: {order.type}")

        existing = await self._lookup_order(order.client_order_id)
        if existing is not None:
            return self._result_from_order(existing, order)

        payload = await self._order_payload(order)
        submit_path = "/api/v1/crypto/trading/orders/"
        for attempt in range(self._max_retries + 1):
            try:
                raw = await self._request("POST", submit_path, json_body=payload)
                return self._result_from_order(raw, order)
            except _ValidationError as exc:
                return OrderRejected(reason=str(exc))
            except _AuthError as exc:
                return OrderRejected(reason=f"Robinhood crypto auth failed: {exc}")
            except (_TransientError, ProviderRateLimited) as exc:
                found = await self._lookup_order(order.client_order_id)
                if found is not None:
                    return self._result_from_order(found, order)
                if attempt >= self._max_retries:
                    return OrderRejected(reason=f"transient Robinhood crypto error: {exc}")
                await self._sleep(0.25 * (2**attempt))
        return OrderRejected(reason="Robinhood crypto submit failed")

    async def get_quote(self, symbol: str) -> Quote:
        """Return the current best bid/ask midpoint for a crypto symbol."""

        rejection = self._symbol_rejection(symbol)
        if rejection is not None:
            raise ValueError(rejection.reason)
        path = f"/api/v1/crypto/marketdata/best_bid_ask/?{urlencode({'symbol': symbol})}"
        raw = await self._request("GET", path)
        quote = _first(raw)
        bid = _decimal_field(quote, "bid_price", "bid", "best_bid")
        ask = _decimal_field(quote, "ask_price", "ask", "best_ask")
        price = _decimal_field(quote, "price", "mark_price", "mid_price")
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / Decimal("2")
        if price is None:
            raise LookupError(f"Robinhood crypto quote missing price for {symbol}")
        return Quote(symbol=symbol, price=price, ts=self._clock(), source="robinhood_crypto")

    async def cancel(self, client_order_id: str) -> bool:
        """Cancel an open order by Sentinel client order id."""

        raw = await self._lookup_order(client_order_id)
        if raw is None:
            return False
        broker_order_id = _string_field(raw, "id", "order_id", "broker_order_id")
        if broker_order_id is None:
            return False
        path = f"/api/v1/crypto/trading/orders/{broker_order_id}/cancel/"
        try:
            await self._request("POST", path)
        except _RobinhoodError:
            return False
        return True

    async def open_orders(self) -> list[object]:
        """Return broker orders that are still working for WS13 reconciliation."""

        raw = await self._request("GET", "/api/v1/crypto/trading/orders/")
        orders = _items(raw)
        return [self._normalize_order(item) for item in orders if _status(item) in _WORKING]

    async def positions(self) -> list[object]:
        """Return Robinhood crypto holdings for reconciliation/live panel use only."""

        raw = await self._request("GET", "/api/v1/crypto/trading/holdings/")
        return [_normalize_holding(item) for item in _items(raw)]

    def capabilities(self) -> set[str]:
        """Advertise the single live asset class supported by this rail."""

        return {"crypto"}

    def _sign_headers(
        self,
        method: str,
        path: str,
        body: str = "",
        *,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        """Backwards-compatible signing helper used by tests."""

        return sign_headers(self.settings, method, path, body, timestamp=timestamp, clock=self._clock)

    async def _lookup_order(self, client_order_id: str) -> JsonObject | None:
        path = f"/api/v1/crypto/trading/orders/?{urlencode({'client_order_id': client_order_id})}"
        try:
            raw = await self._request("GET", path)
        except _RobinhoodError:
            return None
        for item in _items(raw):
            if _string_field(item, "client_order_id", "client_order_id") == client_order_id:
                return item
        return None

    async def _order_payload(self, order: Order) -> JsonObject:
        side = "buy" if order.side == "buy" else "sell"
        payload: JsonObject = {
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": side,
            "quantity": str(order.qty),
        }
        if order.type == "market":
            quote = await self.get_quote(order.symbol)
            buffer = Decimal("1.001") if side == "buy" else Decimal("0.999")
            payload["type"] = "limit"
            payload["limit_price"] = str((quote.price * buffer).quantize(Decimal("0.01")))
        else:
            payload["type"] = "limit"
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> JsonObject:
        await self._throttle()
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), sort_keys=True)
        headers = self._sign_headers(method, path, body)
        headers["content-type"] = "application/json"
        requester = getattr(self._client, "request", None)
        if requester is None:
            raise RuntimeError("Robinhood crypto client must expose request()")
        result = requester(
            method,
            f"{self.settings.base_url}{path}",
            headers=headers,
            content=body.encode() if body else None,
            timeout=self.settings.request_timeout_seconds,
        )
        response = await result if inspect.isawaitable(result) else result
        return await _decode_response(response)

    async def _throttle(self) -> None:
        if self._min_interval_seconds <= 0:
            return
        now = self._clock().timestamp()
        wait = self._min_interval_seconds - (now - self._last_request_at)
        if wait > 0:
            await self._sleep(wait)
        self._last_request_at = self._clock().timestamp()

    def _result_from_order(self, raw: Mapping[str, object], order: Order) -> Fill | OrderPending:
        normalized = self._normalize_order(raw)
        status = _status(raw)
        broker_order_id = _string_field(raw, "id", "order_id", "broker_order_id")
        if status in _TERMINAL_FILLED:
            price = _decimal_field(raw, "average_price", "avg_fill_price", "price", "executed_price")
            return Fill(
                order_id=order.order_id,
                price=price or Decimal("0"),
                qty=_decimal_field(raw, "filled_quantity", "filled_qty", "quantity") or order.qty,
                ts=self._clock(),
                slippage_usd=Decimal("0"),
                commission_usd=Decimal("0"),
                venue="robinhood_crypto",
                broker_order_id=broker_order_id,
            )
        return OrderPending(
            client_order_id=order.client_order_id,
            venue="robinhood_crypto",
            broker_order_id=cast(str | None, normalized.get("broker_order_id")),
        )

    def _normalize_order(self, raw: Mapping[str, object]) -> JsonObject:
        return {
            **dict(raw),
            "client_order_id": _string_field(raw, "client_order_id") or "",
            "broker_order_id": _string_field(raw, "id", "order_id", "broker_order_id"),
            "status": _status(raw),
            "venue": "robinhood_crypto",
            "filled_qty": str(_decimal_field(raw, "filled_quantity", "filled_qty") or Decimal("0")),
            "avg_fill_price": str(_decimal_field(raw, "average_price", "avg_fill_price", "price") or Decimal("0")),
        }

    def _symbol_rejection(self, symbol: str) -> OrderRejected | None:
        if symbol not in self._allowed_symbols:
            return _reject("VENUE_CAPABILITY_MISSING", f"unsupported robinhood crypto symbol: {symbol}")
        return None


class _RobinhoodError(RuntimeError):
    pass


class _AuthError(_RobinhoodError):
    pass


class _ValidationError(_RobinhoodError):
    pass


class _TransientError(_RobinhoodError):
    pass


class ProviderRateLimited(_TransientError):
    """Transient provider throttle that should be retried with bounded backoff."""


def _reject(code: ViolationCode, reason: str) -> OrderRejected:
    return OrderRejected(reason=reason, code=code)


def _normalize_holding(raw: Mapping[str, object]) -> JsonObject:
    symbol = _string_field(raw, "symbol")
    if symbol is None:
        asset = _string_field(raw, "asset_code", "asset", "currency", "code")
        if asset is not None:
            asset_symbol = asset.upper()
            symbol = asset_symbol if "-" in asset_symbol else f"{asset_symbol}-USD"
    qty = _decimal_field(raw, "qty", "quantity", "total_quantity", "amount")
    return {
        **dict(raw),
        "symbol": symbol or "",
        "qty": qty or Decimal("0"),
        "venue": "robinhood_crypto",
    }


def sign_headers(
    settings: RobinhoodSettings,
    method: str,
    path: str,
    body: str = "",
    *,
    timestamp: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, str]:
    """Return Robinhood Crypto API Ed25519 signing headers."""

    api_key = os.environ.get(settings.api_key_env)
    private_key = os.environ.get(settings.private_key_env)
    if not api_key or not private_key:
        raise RuntimeError("Robinhood API key and private key environment variables are required")

    now = clock or (lambda: datetime.now(UTC))
    ts = int(now().timestamp()) if timestamp is None else timestamp
    message = f"{api_key}{ts}{path}{method.upper()}{body}".encode()
    signature = base64.b64encode(_ed25519_sign(private_key, message)).decode("ascii")
    return {
        "x-api-key": api_key,
        "x-signature": signature,
        "x-timestamp": str(ts),
    }


def _ed25519_sign(private_key: str, message: bytes) -> bytes:
    try:
        serialization = importlib.import_module("cryptography.hazmat.primitives.serialization")
    except ModuleNotFoundError:
        seed = _seed_from_pkcs8_pem(private_key)
        return _ed25519_sign_seed(seed, message)
    load_pem_private_key = cast(Callable[..., object], serialization.__dict__["load_pem_private_key"])
    key = cast(Any, load_pem_private_key(private_key.encode(), password=None))
    return cast(bytes, key.sign(message))


def _seed_from_pkcs8_pem(private_key: str) -> bytes:
    lines = [line.strip() for line in private_key.splitlines() if not line.startswith("-----")]
    der = base64.b64decode("".join(lines))
    marker = bytes.fromhex("0420")
    index = der.rfind(marker)
    if index < 0 or len(der) < index + 34:
        raise RuntimeError("Robinhood signing requires an Ed25519 PKCS8 PEM private key")
    return der[index + 2 : index + 34]


_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, -1, _Q) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)
_B = (
    15112221349535400772501151409588531511454012693041857206046113283949847762202,
    46316835694926478169428394003475163141307993866256225615783033603165251855960,
)


def _ed25519_sign_seed(seed: bytes, message: bytes) -> bytes:
    digest = hashlib.sha512(seed).digest()
    a = _clamp_scalar(digest[:32])
    prefix = digest[32:]
    public = _point_encode(_scalarmult(_B, a))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    encoded_r = _point_encode(_scalarmult(_B, r))
    h = int.from_bytes(hashlib.sha512(encoded_r + public + message).digest(), "little") % _L
    s = (r + h * a) % _L
    return encoded_r + s.to_bytes(32, "little")


def _clamp_scalar(raw: bytes) -> int:
    data = bytearray(raw)
    data[0] &= 248
    data[31] &= 63
    data[31] |= 64
    return int.from_bytes(data, "little")


def _point_add(point: tuple[int, int], other: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = point
    x2, y2 = other
    denom_x = pow(1 + _D * x1 * x2 * y1 * y2, -1, _Q)
    denom_y = pow(1 - _D * x1 * x2 * y1 * y2, -1, _Q)
    x3 = (x1 * y2 + x2 * y1) * denom_x % _Q
    y3 = (y1 * y2 + x1 * x2) * denom_y % _Q
    return x3, y3


def _scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _point_encode(point: tuple[int, int]) -> bytes:
    x, y = point
    encoded = bytearray(y.to_bytes(32, "little"))
    encoded[31] |= (x & 1) << 7
    return bytes(encoded)


def _build_httpx_client() -> object:
    import httpx

    return httpx.AsyncClient()


async def _decode_response(response: object) -> JsonObject:
    status_code = int(getattr(response, "status_code", 200))
    if status_code in {401, 403}:
        raise _AuthError(_safe_error(response))
    if status_code in {400, 422}:
        raise _ValidationError(_safe_error(response))
    if status_code in _TRANSIENT_STATUSES:
        if status_code == 429:
            raise ProviderRateLimited(_safe_error(response))
        raise _TransientError(_safe_error(response))
    if status_code >= 400:
        raise _RobinhoodError(_safe_error(response))
    json_method = getattr(response, "json", None)
    if json_method is None:
        if isinstance(response, Mapping):
            return dict(cast(Mapping[str, object], response))
        return {}
    value = json_method()
    decoded = await value if inspect.isawaitable(value) else value
    return cast(JsonObject, decoded if isinstance(decoded, dict) else {"results": decoded})


def _safe_error(response: object) -> str:
    try:
        text = str(cast(Any, response).text)
    except Exception:
        text = ""
    return text[:300] if text else f"HTTP {getattr(response, 'status_code', 'error')}"


def _items(raw: Mapping[str, object]) -> list[JsonObject]:
    results = raw.get("results") or raw.get("data") or raw.get("items")
    if isinstance(results, list):
        return [
            dict(cast(Mapping[str, object], item))
            for item in cast(list[object], results)
            if isinstance(item, Mapping)
        ]
    return [dict(raw)]


def _first(raw: Mapping[str, object]) -> Mapping[str, object]:
    return _items(raw)[0] if _items(raw) else raw


def _status(raw: Mapping[str, object]) -> str:
    value = _string_field(raw, "state", "status")
    return (value or "working").lower()


def _string_field(raw: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = raw.get(name)
        if value is not None:
            return str(value)
    return None


def _decimal_field(raw: Mapping[str, object], *names: str) -> Decimal | None:
    for name in names:
        value = raw.get(name)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None

"""Inert Robinhood crypto broker scaffold."""

from __future__ import annotations

import base64
import importlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sentinel.config.settings import RobinhoodSettings
from sentinel.core.models import Fill, Order, Quote
from sentinel.execution.broker import Broker, OrderRejected

_ALLOWED_SYMBOLS = {"BTC-USD", "ETH-USD"}
_NOT_WIRED = "RobinhoodCryptoBroker scaffold is not wired for trading yet"


class RobinhoodCryptoBroker(Broker):
    """Default-disabled, crypto-only Robinhood scaffold with no live order path."""

    def __init__(
        self,
        settings: RobinhoodSettings | None = None,
        *,
        client: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or RobinhoodSettings()
        if not self.settings.enabled:
            raise RuntimeError("RobinhoodCryptoBroker is disabled")
        if not self.settings.crypto_only:
            raise ValueError("RobinhoodCryptoBroker only supports crypto-only mode")
        self._allowed_symbols = set(self.settings.allowed_symbols) & _ALLOWED_SYMBOLS
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def submit(self, order: Order) -> Fill | OrderRejected:
        """Validate the crypto symbol, then stop before any live order call."""

        rejection = self._symbol_rejection(order.symbol)
        if rejection is not None:
            return rejection
        raise NotImplementedError(_NOT_WIRED)

    async def get_quote(self, symbol: str) -> Quote:
        """Validate the crypto symbol, then stop before any live quote call."""

        rejection = self._symbol_rejection(symbol)
        if rejection is not None:
            raise ValueError(rejection.reason)
        raise NotImplementedError(_NOT_WIRED)

    def _symbol_rejection(self, symbol: str) -> OrderRejected | None:
        if symbol not in self._allowed_symbols:
            return OrderRejected(reason=f"unsupported robinhood crypto symbol: {symbol}")
        return None

    def _sign_headers(
        self,
        method: str,
        path: str,
        body: str = "",
        *,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        api_key = os.environ.get(self.settings.api_key_env)
        private_key = os.environ.get(self.settings.private_key_env)
        if not api_key or not private_key:
            raise RuntimeError("Robinhood API key and private key environment variables are required")

        try:
            serialization = importlib.import_module("cryptography.hazmat.primitives.serialization")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Robinhood signing requires the optional 'robinhood' extra"
            ) from exc

        ts = int(self._clock().timestamp()) if timestamp is None else timestamp
        message = f"{api_key}{ts}{path}{method.upper()}{body}".encode()
        load_pem_private_key = cast(Callable[..., object], serialization.__dict__["load_pem_private_key"])
        key = cast(Any, load_pem_private_key(private_key.encode(), password=None))
        signature = base64.b64encode(cast(bytes, key.sign(message))).decode("ascii")
        return {
            "x-api-key": api_key,
            "x-signature": signature,
            "x-timestamp": str(ts),
        }

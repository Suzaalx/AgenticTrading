"""Broker interfaces for Sentinel execution."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from sentinel.core.models import Fill, Order, Quote, ViolationCode

Venue = Literal["paper", "robinhood_crypto", "robinhood_agentic"]


class OrderRejected(BaseModel):
    """A deterministic refusal to fill an order."""

    model_config = ConfigDict(frozen=True)

    reason: str
    code: ViolationCode | None = None


class OrderPending(BaseModel):
    """A live broker acknowledgement for an order whose fill arrives asynchronously."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    venue: Venue
    broker_order_id: str | None = None
    status: Literal["working"] = "working"


class QuoteSource(Protocol):
    """Minimal quote dependency injected into brokers."""

    def get_quote(self, symbol: str) -> Quote | None | Awaitable[Quote | None]: ...


class Broker(Protocol):
    """Common broker contract implemented by paper and future live brokers."""

    async def submit(self, order: Order) -> Fill | OrderRejected: ...

    async def get_quote(self, symbol: str) -> Quote: ...


class LiveBroker(Protocol):
    """Live broker contract for asynchronous rails and reconciliation."""

    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending: ...

    async def cancel(self, client_order_id: str) -> bool: ...

    async def open_orders(self) -> list[object]: ...

    async def positions(self) -> list[object]: ...

    def capabilities(self) -> set[str]: ...

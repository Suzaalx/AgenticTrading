"""Broker interfaces for Sentinel execution."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from sentinel.core.models import Fill, Order, Quote


class OrderRejected(BaseModel):
    """A deterministic refusal to fill an order."""

    model_config = ConfigDict(frozen=True)

    reason: str


class QuoteSource(Protocol):
    """Minimal quote dependency injected into brokers."""

    def get_quote(self, symbol: str) -> Quote | None | Awaitable[Quote | None]: ...


class Broker(Protocol):
    """Common broker contract implemented by paper and future live brokers."""

    async def submit(self, order: Order) -> Fill | OrderRejected: ...

    async def get_quote(self, symbol: str) -> Quote: ...

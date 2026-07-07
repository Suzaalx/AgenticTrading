"""Paper broker implementation."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sentinel.config.settings import load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import OrderFilled, OrderSubmitted
from sentinel.core.models import Fill, Order, Quote
from sentinel.execution.broker import Broker, OrderRejected, QuoteSource

_MARKET_TZ = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)


class PaperBroker(Broker):
    """Deterministic paper broker that fills market orders from an injected quote source."""

    def __init__(
        self,
        quote_source: QuoteSource,
        *,
        bus: EventBus | None = None,
        slippage_bps: int | None = None,
        commission_usd: Decimal | None = None,
        stale_after: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        settings = load_settings().execution
        self._quote_source = quote_source
        self._bus = bus
        self._slippage_bps = slippage_bps if slippage_bps is not None else settings.slippage_bps
        self._commission_usd = (
            commission_usd
            if commission_usd is not None
            else Decimal(str(settings.commission_usd))
        )
        self._stale_after = stale_after
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote from the injected quote source."""

        maybe_quote = self._quote_source.get_quote(symbol)
        if inspect.isawaitable(maybe_quote):
            quote = await maybe_quote
        else:
            quote = maybe_quote
        if quote is None:
            raise LookupError(symbol)
        return quote

    async def submit(self, order: Order) -> Fill | OrderRejected:
        """Submit a market order and synchronously produce a paper fill or rejection."""

        if self._bus is not None:
            await self._bus.publish(OrderSubmitted(order=order))
        if order.type != "market":
            return OrderRejected(reason="paper broker only supports market orders")

        quote_result = await self._quote_or_rejection(order.symbol)
        if isinstance(quote_result, OrderRejected):
            return quote_result
        quote = quote_result
        now = _ensure_aware_utc(self._clock())
        if _is_market_hours(now) and now - _ensure_aware_utc(quote.ts) > self._stale_after:
            return OrderRejected(reason=f"stale quote for {order.symbol}")

        fill = Fill(
            order_id=order.order_id,
            price=self._slipped_price(quote.price, order.side),
            qty=order.qty,
            ts=now,
            slippage_usd=self._slippage_usd(quote.price, order.qty),
            commission_usd=self._commission_usd,
        )
        if self._bus is not None:
            await self._bus.publish(OrderFilled(order_id=order.order_id, fill=fill))
        return fill

    async def _quote_or_rejection(self, symbol: str) -> Quote | OrderRejected:
        try:
            quote = await self.get_quote(symbol)
        except Exception as exc:
            return OrderRejected(reason=f"no quote available for {symbol}: {exc}")
        return quote

    def _slippage_rate(self) -> Decimal:
        return Decimal(self._slippage_bps) / Decimal("10000")

    def _slipped_price(self, quote_price: Decimal, side: str) -> Decimal:
        rate = self._slippage_rate()
        if side == "buy":
            return quote_price * (Decimal("1") + rate)
        return quote_price * (Decimal("1") - rate)

    def _slippage_usd(self, quote_price: Decimal, qty: Decimal) -> Decimal:
        return quote_price * qty * self._slippage_rate()


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_market_hours(value: datetime) -> bool:
    local = _ensure_aware_utc(value).astimezone(_MARKET_TZ)
    return local.weekday() < 5 and _MARKET_OPEN <= local.time() <= _MARKET_CLOSE

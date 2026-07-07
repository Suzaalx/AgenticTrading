"""Paper broker implementation."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from sentinel.config.settings import load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import OrderFilled, OrderSubmitted
from sentinel.core.models import Fill, OptionContract, OptionQuote, Order, Quote
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
        self._fill_quote_ts_by_order_id: dict[str, datetime] = {}

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
        if order.asset_type == "option" or order.legs is not None:
            return await self._submit_option(order)
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
        self._fill_quote_ts_by_order_id[order.order_id] = _ensure_aware_utc(quote.ts)
        if self._bus is not None:
            await self._bus.publish(OrderFilled(order_id=order.order_id, fill=fill))
        return fill

    def quote_ts_for_order(self, order_id: str) -> datetime | None:
        """Return the source quote timestamp used to price a paper fill."""

        return self._fill_quote_ts_by_order_id.get(order_id)

    async def _submit_option(self, order: Order) -> Fill | OrderRejected:
        """Fill options with Fill.price as signed total premium, not per-contract price."""

        if order.asset_type != "option":
            return OrderRejected(reason="option legs require asset_type=option")
        if not order.legs:
            return OrderRejected(reason="option order requires legs")
        if order.type not in {"net_debit", "net_credit"}:
            return OrderRejected(reason="option paper broker only supports net_debit/net_credit orders")

        now = _ensure_aware_utc(self._clock())
        quotes: list[OptionQuote] = []
        for leg in order.legs:
            quote_result = await self._option_quote_or_rejection(leg.contract)
            if isinstance(quote_result, OrderRejected):
                return quote_result
            quote = quote_result
            if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
                return OrderRejected(reason=f"missing bid/ask for {leg.contract.contract_symbol}")
            if _is_market_hours(now) and now - _ensure_aware_utc(quote.ts) > self._stale_after:
                return OrderRejected(reason=f"stale quote for {leg.contract.contract_symbol}")
            quotes.append(quote)

        premium = Decimal("0")
        slippage = Decimal("0")
        for leg, quote in zip(order.legs, quotes, strict=True):
            mid = (quote.bid + quote.ask) / Decimal("2")
            half_spread = (quote.ask - quote.bid) / Decimal("2")
            leg_slip = max(
                half_spread * Decimal(str(load_settings().options.slippage_half_spread_frac)),
                Decimal("0.01"),
            )
            fill_price = mid + leg_slip if leg.side == "buy" else mid - leg_slip
            sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
            contracts = Decimal(leg.contracts)
            multiplier = Decimal(leg.contract.multiplier)
            premium += sign * fill_price * contracts * multiplier
            slippage += leg_slip * contracts * multiplier

        if premium > 0 and order.type != "net_debit":
            return OrderRejected(reason="net debit option legs require type=net_debit")
        if premium < 0 and order.type != "net_credit":
            return OrderRejected(reason="net credit option legs require type=net_credit")
        if premium == 0:
            return OrderRejected(reason="zero-premium option order is not supported")

        commission = Decimal(str(load_settings().options.commission_per_contract_usd)) * sum(
            (Decimal(leg.contracts) for leg in order.legs), Decimal("0")
        )
        fill = Fill(
            order_id=order.order_id,
            price=premium,
            qty=order.qty,
            ts=now,
            slippage_usd=slippage,
            commission_usd=commission,
        )
        self._fill_quote_ts_by_order_id[order.order_id] = max(
            _ensure_aware_utc(quote.ts) for quote in quotes
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

    async def _option_quote_or_rejection(self, contract: OptionContract) -> OptionQuote | OrderRejected:
        source = cast(object, self._quote_source)
        getter = getattr(source, "get_option_quote", None)
        if getter is None:
            getter = getattr(source, "get_quote", None)
        if getter is None:
            return OrderRejected(reason=f"no option quote available for {contract.contract_symbol}")
        try:
            try:
                maybe_quote = getter(contract)
            except TypeError:
                maybe_quote = getter(contract.contract_symbol)
            quote = await maybe_quote if inspect.isawaitable(maybe_quote) else maybe_quote
        except Exception as exc:
            return OrderRejected(reason=f"no option quote available for {contract.contract_symbol}: {exc}")
        if not isinstance(quote, OptionQuote):
            return OrderRejected(reason=f"no option quote available for {contract.contract_symbol}")
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

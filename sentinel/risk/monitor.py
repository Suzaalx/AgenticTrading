"""Mechanical stop, take-profit, and time-exit monitoring."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from sentinel.config.settings import load_settings
from sentinel.core.ids import new_id
from sentinel.core.models import OptionLeg, OptionPosition, Order, Position
from sentinel.options.strategies import QuoteMap, net_premium
from sentinel.risk.audit import append_audit
from sentinel.risk.killswitch import is_engaged

ExitReason = Literal["stop_loss", "take_profit", "time_exit"]
PositionsProvider = Callable[[], Iterable[Position] | Awaitable[Iterable[Position]]]
MarksProvider = Callable[[], Mapping[str, Decimal] | Awaitable[Mapping[str, Decimal]]]
OptionPositionsProvider = Callable[[], Iterable[OptionPosition] | Awaitable[Iterable[OptionPosition]]]
OptionMarksProvider = Callable[
    [OptionPosition], Mapping[str, Decimal] | Awaitable[Mapping[str, Decimal]]
]
OrderHandler = Callable[[Order], bool | None | Awaitable[bool | None]]
Clock = Callable[[], datetime]


def _exit_side(position: Position) -> Literal["buy", "sell"]:
    return "sell" if position.qty > 0 else "buy"


def _exit_order(position: Position, now: datetime, reason: ExitReason) -> Order:
    return Order(
        order_id=new_id(),
        run_id=position.source_run_id,
        symbol=position.symbol,
        side=_exit_side(position),
        qty=abs(position.qty),
        type="market",
        reason=reason,
        created_at=now,
    )


def evaluate_position(position: Position, mark: Decimal, now: datetime) -> Order | None:
    """Evaluate one position and return a mechanical exit order when triggered."""

    if position.qty == 0 or position.avg_cost <= 0 or mark <= 0:
        return None

    if position.stop_loss_pct is not None:
        stop_pct = Decimal(str(position.stop_loss_pct)) / Decimal("100")
        if position.qty > 0 and mark <= position.avg_cost * (Decimal("1") - stop_pct):
            return _exit_order(position, now, "stop_loss")
        if position.qty < 0 and mark >= position.avg_cost * (Decimal("1") + stop_pct):
            return _exit_order(position, now, "stop_loss")

    if position.take_profit_pct is not None:
        take_pct = Decimal(str(position.take_profit_pct)) / Decimal("100")
        if position.qty > 0 and mark >= position.avg_cost * (Decimal("1") + take_pct):
            return _exit_order(position, now, "take_profit")
        if position.qty < 0 and mark <= position.avg_cost * (Decimal("1") - take_pct):
            return _exit_order(position, now, "take_profit")

    if position.horizon_days is not None and now >= position.opened_at + timedelta(days=position.horizon_days):
        return _exit_order(position, now, "time_exit")

    return None


def evaluate_option_position(
    position: OptionPosition,
    marks: Mapping[str, Decimal],
    now: datetime,
    force_close_dte: int | None = None,
) -> Order | None:
    """Evaluate one option strategy and return a lifecycle close order when triggered."""

    current = net_premium(position.legs, cast(QuoteMap, marks))
    basis = abs(position.open_premium)
    if basis > 0:
        pnl = current - position.open_premium
        if position.stop_loss_pct_premium is not None:
            stop = basis * Decimal(str(position.stop_loss_pct_premium)) / Decimal("100")
            if pnl <= -stop:
                return _option_close_order(position, marks, now, "stop_loss")
        if position.take_profit_pct_premium is not None:
            take = basis * Decimal(str(position.take_profit_pct_premium)) / Decimal("100")
            if pnl >= take:
                return _option_close_order(position, marks, now, "take_profit")

    if position.horizon_days is not None and now >= position.opened_at + timedelta(days=position.horizon_days):
        return _option_close_order(position, marks, now, "time_exit")

    dte = (position.expiry - now.date()).days
    if dte <= (force_close_dte if force_close_dte is not None else load_settings().options.force_close_dte):
        return _option_close_order(position, marks, now, "time_exit")

    return None


def expiry_settlement_order(
    position: OptionPosition,
    marks: Mapping[str, Decimal],
    now: datetime,
) -> Order:
    """Return an audit/order record for deterministic paper expiry settlement."""

    settlement_value = _settlement_value(position, _underlying_mark(position, marks))
    return Order(
        order_id=new_id(),
        run_id=position.source_run_id,
        symbol=position.underlying,
        side="sell" if settlement_value >= 0 else "buy",
        qty=abs(settlement_value),
        type="market",
        reason="expiry_settlement",
        created_at=now,
        asset_type="option",
        legs=[leg.model_copy(deep=True) for leg in position.legs],
        strategy=position.strategy,
    )


def _option_close_order(
    position: OptionPosition,
    marks: Mapping[str, Decimal],
    now: datetime,
    reason: ExitReason,
) -> Order:
    legs = [_reverse_leg(leg) for leg in position.legs]
    premium = net_premium(legs, cast(QuoteMap, marks))
    return Order(
        order_id=new_id(),
        run_id=position.source_run_id,
        symbol=position.underlying,
        side="buy" if premium > 0 else "sell",
        qty=Decimal(max(leg.contracts for leg in legs)),
        type="net_debit" if premium > 0 else "net_credit",
        reason=reason,
        created_at=now,
        asset_type="option",
        legs=legs,
        strategy=position.strategy,
    )


def _reverse_leg(leg: OptionLeg) -> OptionLeg:
    return leg.model_copy(update={"side": "sell" if leg.side == "buy" else "buy"})


def _has_all_leg_marks(position: OptionPosition, marks: Mapping[str, Decimal]) -> bool:
    return all(leg.contract.contract_symbol in marks for leg in position.legs)


def _underlying_mark(position: OptionPosition, marks: Mapping[str, Decimal]) -> Decimal:
    mark = marks.get(position.underlying)
    if mark is None:
        msg = f"missing underlying mark for {position.underlying}"
        raise ValueError(msg)
    return mark


def _settlement_value(position: OptionPosition, underlying: Decimal) -> Decimal:
    total = Decimal("0")
    for leg in position.legs:
        if leg.contract.kind == "call":
            intrinsic = max(underlying - leg.contract.strike, Decimal("0"))
        else:
            intrinsic = max(leg.contract.strike - underlying, Decimal("0"))
        sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
        total += sign * intrinsic * Decimal(leg.contracts) * Decimal(leg.contract.multiplier)
    return total


class PositionMonitor:
    """Async wrapper that evaluates positions and emits mechanical exit orders."""

    def __init__(
        self,
        positions_provider: PositionsProvider,
        marks_provider: MarksProvider,
        on_order: OrderHandler,
        *,
        interval_seconds: float = 300.0,
        clock: Clock | None = None,
        option_positions_provider: OptionPositionsProvider | None = None,
        option_marks_provider: OptionMarksProvider | None = None,
        on_option_order: OrderHandler | None = None,
        force_close_dte: int | None = None,
    ) -> None:
        self._positions_provider = positions_provider
        self._marks_provider = marks_provider
        self._on_order = on_order
        self._interval_seconds = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._option_positions_provider = option_positions_provider
        self._option_marks_provider = option_marks_provider
        self._on_option_order = on_option_order or on_order
        self._force_close_dte = (
            force_close_dte
            if force_close_dte is not None
            else load_settings().options.force_close_dte
        )
        self.notes: list[str] = []

    async def _positions(self) -> Iterable[Position]:
        positions = self._positions_provider()
        if isinstance(positions, Awaitable):
            return await positions
        return positions

    async def _marks(self) -> Mapping[str, Decimal]:
        marks = self._marks_provider()
        if isinstance(marks, Awaitable):
            return await marks
        return marks

    async def _option_positions(self) -> Iterable[OptionPosition]:
        if self._option_positions_provider is None:
            return []
        positions = self._option_positions_provider()
        if isinstance(positions, Awaitable):
            return await positions
        return positions

    async def _option_marks(self, position: OptionPosition) -> Mapping[str, Decimal]:
        if self._option_marks_provider is None:
            return {}
        marks = self._option_marks_provider(position)
        if isinstance(marks, Awaitable):
            return await marks
        return marks

    async def _handle_order(self, order: Order) -> bool:
        handled = self._on_order(order)
        if isinstance(handled, Awaitable):
            handled = await handled
        return handled is not False

    async def _handle_option_order(self, order: Order) -> bool:
        handled = self._on_option_order(order)
        if isinstance(handled, Awaitable):
            handled = await handled
        return handled is not False

    async def run_once(self, now: datetime | None = None) -> list[Order]:
        """Evaluate all positions once and pass triggered exits to the order handler."""

        evaluation_time = now or self._clock()
        marks = await self._marks()
        orders: list[Order] = []
        for position in await self._positions():
            mark = marks.get(position.symbol)
            if mark is None:
                continue
            order = evaluate_position(position, mark, evaluation_time)
            if order is None:
                continue
            orders.append(order)
            await self._handle_order(order)
        orders.extend(await self._run_options_once(evaluation_time))
        return orders

    async def _run_options_once(self, now: datetime) -> list[Order]:
        if self._option_positions_provider is None:
            return []
        if is_engaged():
            self.notes.append("kill switch engaged; skipping option lifecycle execution")
            return []
        orders: list[Order] = []
        for position in await self._option_positions():
            try:
                marks = await self._option_marks(position)
            except Exception as exc:
                self.notes.append(f"skipped {position.position_id}: option mark fetch failed: {exc}")
                continue
            if not _has_all_leg_marks(position, marks):
                self.notes.append(f"skipped {position.position_id}: missing option mark")
                continue
            order = evaluate_option_position(position, marks, now, self._force_close_dte)
            if order is None:
                continue
            orders.append(order)
            close_ok = True
            try:
                close_ok = await self._handle_option_order(order)
            except Exception as exc:
                close_ok = False
                self.notes.append(f"close failed for {position.position_id}: {exc}")
            if not close_ok and now.date() >= position.expiry:
                settlement = expiry_settlement_order(position, marks, now)
                orders.append(settlement)
                append_audit(
                    "option_expiry_settlement",
                    {
                        "position_id": position.position_id,
                        "underlying": position.underlying,
                        "strategy": position.strategy,
                        "settlement_value": _settlement_value(position, _underlying_mark(position, marks)),
                        "order": settlement,
                    },
                    actor="position_monitor",
                )
        return orders

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Run until cancelled or until an optional stop event is set."""

        while stop_event is None or not stop_event.is_set():
            await self.run_once()
            await asyncio.sleep(self._interval_seconds)

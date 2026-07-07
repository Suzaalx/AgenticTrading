"""Mechanical stop, take-profit, and time-exit monitoring."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sentinel.core.ids import new_id
from sentinel.core.models import Order, Position

ExitReason = Literal["stop_loss", "take_profit", "time_exit"]
PositionsProvider = Callable[[], Iterable[Position] | Awaitable[Iterable[Position]]]
MarksProvider = Callable[[], Mapping[str, Decimal] | Awaitable[Mapping[str, Decimal]]]
OrderHandler = Callable[[Order], None | Awaitable[None]]
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
    ) -> None:
        self._positions_provider = positions_provider
        self._marks_provider = marks_provider
        self._on_order = on_order
        self._interval_seconds = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

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

    async def _handle_order(self, order: Order) -> None:
        handled = self._on_order(order)
        if isinstance(handled, Awaitable):
            await handled

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
        return orders

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Run until cancelled or until an optional stop event is set."""

        while stop_event is None or not stop_event.is_set():
            await self.run_once()
            await asyncio.sleep(self._interval_seconds)

"""Live-trading mandate overlay for orders routed to live venues."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sentinel.core.events import GateEvaluated
from sentinel.core.models import (
    GateResult,
    Mandate,
    MandateLive,
    Order,
    Quote,
    Violation,
    ViolationCode,
)
from sentinel.risk._events import emit_event
from sentinel.risk.audit import append_audit

_STAGE_FLAG_BY_ASSET = {
    "crypto": "crypto_stage_enabled",
    "equity": "equity_stage_enabled",
    "option": "options_stage_enabled",
}


class LiveMandate:
    """Strict overlay applied only after the normal gate and only for live orders."""

    def __init__(self, mandate: Mandate | MandateLive) -> None:
        self.mandate = mandate
        self.live = mandate.live if isinstance(mandate, Mandate) else mandate

    def check_live_order(self, order: Order, **context: object) -> GateResult:
        """Check live-only controls using broker/reconciliation context supplied by callers."""

        violations: list[Violation] = []
        now = _coerce_datetime(context.get("now")) or datetime.now(UTC)
        notional = _order_notional(order, context)

        stage_flag = _STAGE_FLAG_BY_ASSET[order.asset_type]
        if not bool(getattr(self.live, stage_flag)):
            violations.append(
                _violation(
                    "LIVE_STAGE_NOT_ENABLED",
                    f"live {order.asset_type} stage is not enabled",
                )
            )

        if self.live.require_limit_orders and order.type == "market":
            violations.append(
                _violation(
                    "LIVE_STAGE_NOT_ENABLED",
                    "live market orders are disabled; convert to a marketable limit first",
                )
            )

        reconciliation_unavailable = (
            bool(context.get("reconciliation_stale", False)) or _reconciliation_ticks(context) > 2
        )
        if reconciliation_unavailable:
            violations.append(
                _violation(
                    "RECONCILIATION_MISMATCH",
                    "broker reconciliation data is stale; live risk is halted",
                )
            )

        max_notional = Decimal(str(self.live.max_live_order_notional_usd))
        if notional is None:
            violations.append(
                _violation(
                    "LIVE_NOTIONAL_EXCEEDED",
                    "live order notional cannot be evaluated; live risk is halted",
                )
            )
        elif notional > max_notional:
            violations.append(
                _violation(
                    "LIVE_NOTIONAL_EXCEEDED",
                    f"live order notional {notional} exceeds {max_notional}",
                )
            )

        live_orders_today = _coerce_int(context.get("live_orders_today"))
        if live_orders_today is None:
            violations.append(
                _violation(
                    "LIVE_ORDER_LIMIT",
                    "live daily order count cannot be evaluated; live risk is halted",
                )
            )
            live_orders_today = 0
        elif live_orders_today >= self.live.max_live_orders_per_day:
            violations.append(
                _violation(
                    "LIVE_ORDER_LIMIT",
                    f"live daily order limit of {self.live.max_live_orders_per_day} reached",
                )
            )

        quote_ts = _quote_ts(order, context)
        if quote_ts is None:
            violations.append(
                _violation(
                    "QUOTE_TOO_OLD",
                    "venue quote timestamp is unavailable; live risk is halted",
                )
            )
        else:
            quote_age = abs((now - _align_tz(quote_ts, now)).total_seconds())
            if quote_age > self.live.quote_max_age_seconds:
                violations.append(
                    _violation(
                        "QUOTE_TOO_OLD",
                        f"venue quote age {quote_age:.0f}s exceeds {self.live.quote_max_age_seconds}s",
                    )
                )

        if not reconciliation_unavailable:
            day_pnl = _day_pnl(context)
            if day_pnl is None:
                violations.append(
                    _violation(
                        "RECONCILIATION_MISMATCH",
                        "broker day P&L is unavailable; live risk is halted",
                    )
                )
            elif day_pnl <= -Decimal(str(self.live.max_live_daily_loss_usd)):
                violations.append(
                    _violation(
                        "LIVE_DAILY_LOSS_HALT",
                        f"broker day P&L {day_pnl} breached live daily loss limit",
                    )
                )

        if "live_positions" not in context:
            allocation = Decimal("0")
            violations.append(
                _violation(
                    "RECONCILIATION_MISMATCH",
                    "live positions are unavailable; live risk is halted",
                )
            )
        else:
            allocation = _positions_notional(context.get("live_positions"))
        if notional is not None:
            allocation += notional
        max_allocation = Decimal(str(self.live.max_account_allocation_usd))
        if allocation > max_allocation:
            violations.append(
                _violation(
                    "LIVE_NOTIONAL_EXCEEDED",
                    f"live allocation {allocation} exceeds {max_allocation}",
                )
            )

        result = GateResult(passed=len(violations) == 0, violations=violations)
        append_audit(
            "live_mandate_evaluated",
            {
                "order": order,
                "passed": result.passed,
                "violations": result.violations,
                "notional": notional,
                "live_orders_today": live_orders_today,
                "allocation_after": allocation,
            },
        )
        emit_event(GateEvaluated(run_id=order.run_id or "", result=result))
        return result


def to_marketable_limit(
    order: Order,
    quote: Quote | Mapping[str, object],
    buffer_bps: int = 10,
) -> tuple[Order, Decimal]:
    """Return a limit-order copy and its marketable limit price."""

    price = _quote_side_price(order, quote)
    multiplier = Decimal("1") + (Decimal(buffer_bps) / Decimal("10000"))
    limit_price = price * multiplier if order.side == "buy" else price / multiplier
    return order.model_copy(update={"type": "limit"}), limit_price.quantize(Decimal("0.01"))


def _violation(code: ViolationCode, message: str) -> Violation:
    return Violation(code=code, message=message)


def _order_notional(order: Order, context: Mapping[str, object]) -> Decimal | None:
    direct = _order_direct_notional(order, context)
    if direct is not None:
        return direct
    explicit = _coerce_decimal(
        context.get("order_notional")
        or context.get("order_notional_usd")
        or context.get("notional")
        or context.get("notional_usd")
    )
    if explicit is not None:
        return abs(explicit)
    price = _coerce_decimal(context.get("price") or context.get("mark") or context.get("quote_price"))
    if price is None:
        quote = context.get("quote")
        if isinstance(quote, Quote):
            price = quote.price
        elif isinstance(quote, Mapping):
            quote_map = cast(Mapping[str, object], quote)
            price = _coerce_decimal(
                quote_map.get("price") or quote_map.get("ask") or quote_map.get("bid")
            )
    if price is None:
        return None
    return abs(order.qty * price * _order_multiplier(order, context))


def _order_direct_notional(order: Order, context: Mapping[str, object]) -> Decimal | None:
    if order.legs:
        total = Decimal("0")
        for leg in order.legs:
            if leg.limit_price is None:
                return None
            total += abs(Decimal(leg.contracts) * leg.limit_price * Decimal(leg.contract.multiplier))
        return total
    price = _coerce_decimal(
        _order_attr(order, "limit_price", "mark_price", "mark", "price", "quote_price")
    )
    if price is None:
        return None
    return abs(order.qty * price * _order_multiplier(order, context))


def _order_multiplier(order: Order, context: Mapping[str, object]) -> Decimal:
    multiplier = _coerce_decimal(context.get("multiplier") or _order_attr(order, "multiplier"))
    if multiplier is not None:
        return multiplier
    return Decimal("100") if order.asset_type == "option" else Decimal("1")


def _quote_ts(order: Order, context: Mapping[str, object]) -> datetime | None:
    quote_ts = _coerce_datetime(context.get("quote_ts") or _order_attr(order, "quote_ts", "quote_timestamp"))
    if quote_ts is not None:
        return quote_ts
    quote = context.get("quote") or _order_attr(order, "quote")
    return quote.ts if isinstance(quote, Quote) else None


def _order_attr(order: Order, *names: str) -> object | None:
    for name in names:
        value = getattr(order, name, None)
        if value is not None:
            return value
    return None


def _day_pnl(context: Mapping[str, object]) -> Decimal | None:
    direct = _coerce_decimal(context.get("broker_day_pnl"))
    if direct is None:
        direct = _coerce_decimal(context.get("broker_day_pnl_usd"))
    if direct is not None:
        return direct
    realized = _coerce_decimal(context.get("broker_realized_day_pnl")) or Decimal("0")
    unrealized = _coerce_decimal(context.get("broker_unrealized_day_pnl")) or Decimal("0")
    if "broker_realized_day_pnl" in context or "broker_unrealized_day_pnl" in context:
        return realized + unrealized
    return None


def _reconciliation_ticks(context: Mapping[str, object]) -> int:
    for key in ("reconciliation_stale_ticks", "reconciliation_age_ticks"):
        value = _coerce_int(context.get(key))
        if value is not None:
            return value
    return 0


def _positions_notional(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Mapping):
        value_map = cast(Mapping[str, object], value)
        return sum((_position_notional(item) for item in value_map.values()), Decimal("0"))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        value_sequence = cast(Sequence[object], value)
        return sum((_position_notional(item) for item in value_sequence), Decimal("0"))
    return _position_notional(value)


def _position_notional(position: object) -> Decimal:
    if isinstance(position, Mapping):
        position_map = cast(Mapping[str, object], position)
        for key in ("notional", "notional_usd", "market_value", "market_value_usd"):
            amount = _coerce_decimal(position_map.get(key))
            if amount is not None:
                return abs(amount)
        qty = _coerce_decimal(position_map.get("qty") or position_map.get("quantity"))
        price = _coerce_decimal(
            position_map.get("price") or position_map.get("mark") or position_map.get("avg_cost")
        )
        return abs(qty * price) if qty is not None and price is not None else Decimal("0")
    for key in ("notional", "notional_usd", "market_value", "market_value_usd"):
        amount = _coerce_decimal(getattr(position, key, None))
        if amount is not None:
            return abs(amount)
    qty = _coerce_decimal(getattr(position, "qty", None) or getattr(position, "quantity", None))
    price = _coerce_decimal(getattr(position, "price", None) or getattr(position, "mark", None))
    return abs(qty * price) if qty is not None and price is not None else Decimal("0")


def _quote_side_price(order: Order, quote: Quote | Mapping[str, object]) -> Decimal:
    if isinstance(quote, Quote):
        return quote.price
    key = "ask" if order.side == "buy" else "bid"
    price = _coerce_decimal(quote.get(key) or quote.get("price"))
    if price is None:
        msg = f"quote must include {key!r} or 'price'"
        raise ValueError(msg)
    return price


def _coerce_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _align_tz(ts: datetime, now: datetime) -> datetime:
    if ts.tzinfo is None and now.tzinfo is not None:
        return ts.replace(tzinfo=cast(Any, now.tzinfo))
    if ts.tzinfo is not None and now.tzinfo is None:
        return ts.replace(tzinfo=None)
    return ts

"""Deterministic mandate gate for order safety checks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sentinel.core.events import GateEvaluated
from sentinel.core.models import GateResult, Mandate, Order, Portfolio, Violation, ViolationCode
from sentinel.risk._events import emit_event
from sentinel.risk.audit import append_audit
from sentinel.risk.mandate import is_symbol_allowed

ONE_HUNDRED = Decimal("100")


def _decimal(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _signed_order_qty(order: Order) -> Decimal:
    qty = abs(order.qty)
    return qty if order.side == "buy" else -qty


def _position_qty(portfolio: Portfolio, symbol: str) -> Decimal:
    return sum((position.qty for position in portfolio.positions if position.symbol == symbol), Decimal("0"))


def _price_for(symbol: str, portfolio: Portfolio, marks: dict[str, Decimal] | None) -> Decimal:
    if marks is not None and symbol in marks and marks[symbol] > 0:
        return marks[symbol]
    for position in portfolio.positions:
        if position.symbol == symbol and position.avg_cost > 0:
            return position.avg_cost
    return Decimal("0")


def _equity(portfolio: Portfolio, marks: dict[str, Decimal] | None) -> Decimal:
    return portfolio.marked_equity(marks) if marks is not None else portfolio.equity


def _pct_amount(base: Decimal, pct: float) -> Decimal:
    return base * _decimal(pct) / ONE_HUNDRED


def _is_risk_reducing(order: Order, portfolio: Portfolio) -> bool:
    before = _position_qty(portfolio, order.symbol)
    if before == 0:
        return False
    after = before + _signed_order_qty(order)
    return abs(after) < abs(before)


def _would_open_or_increase_short(order: Order, portfolio: Portfolio) -> bool:
    if order.side != "sell":
        return False
    before = _position_qty(portfolio, order.symbol)
    after = before + _signed_order_qty(order)
    if before >= 0:
        return after < 0
    return after < before


def _gross_exposure_after(
    order: Order,
    portfolio: Portfolio,
    marks: dict[str, Decimal] | None,
) -> Decimal:
    quantities: dict[str, Decimal] = {}
    prices: dict[str, Decimal] = {}
    for position in portfolio.positions:
        quantities[position.symbol] = quantities.get(position.symbol, Decimal("0")) + position.qty
        prices[position.symbol] = _price_for(position.symbol, portfolio, marks)
    quantities[order.symbol] = quantities.get(order.symbol, Decimal("0")) + _signed_order_qty(order)
    prices[order.symbol] = _price_for(order.symbol, portfolio, marks)
    return sum((abs(qty) * prices.get(symbol, Decimal("0")) for symbol, qty in quantities.items()), Decimal("0"))


def _daily_pnl_pct(
    portfolio: Portfolio,
    marks: dict[str, Decimal] | None,
    day_pnl_pct: float | None,
) -> Decimal | None:
    if day_pnl_pct is not None:
        return _decimal(day_pnl_pct)
    equity = _equity(portfolio, marks)
    if equity == 0:
        return None
    return portfolio.day_pnl / equity * ONE_HUNDRED


def _seconds_between(later: datetime, earlier: datetime) -> float:
    if later.tzinfo is None or earlier.tzinfo is None:
        return (later.replace(tzinfo=None) - earlier.replace(tzinfo=None)).total_seconds()
    return (later - earlier).total_seconds()


def _violation(code: ViolationCode, message: str) -> Violation:
    return Violation(code=code, message=message)


def check_order(
    order: Order,
    portfolio: Portfolio,
    mandate: Mandate,
    *,
    orders_today: int,
    last_order_ts: dict[str, datetime] | None,
    kill_engaged: bool,
    marks: dict[str, Decimal] | None,
    day_pnl_pct: float | None,
) -> GateResult:
    """Evaluate every deterministic mandate rule and audit the gate result."""

    violations: list[Violation] = []
    price = _price_for(order.symbol, portfolio, marks)
    equity = _equity(portfolio, marks)
    signed_qty = _signed_order_qty(order)
    current_qty = _position_qty(portfolio, order.symbol)
    resulting_qty = current_qty + signed_qty

    if not is_symbol_allowed(order.symbol, mandate):
        violations.append(
            _violation("SYMBOL_NOT_IN_UNIVERSE", f"{order.symbol} is not in the mandate universe")
        )

    order_notional = abs(order.qty) * price
    max_order_notional = _decimal(mandate.max_order_notional_usd)
    resulting_position_notional = abs(resulting_qty) * price
    max_position_notional = _pct_amount(max(equity, Decimal("0")), mandate.max_position_pct_equity)
    if order_notional > max_order_notional or (
        resulting_qty != 0 and resulting_position_notional > max_position_notional
    ):
        violations.append(
            _violation(
                "ORDER_TOO_LARGE",
                "order notional or resulting position exceeds mandate limits",
            )
        )

    gross_after = _gross_exposure_after(order, portfolio, marks)
    max_gross = _pct_amount(max(equity, Decimal("0")), mandate.max_gross_exposure_pct)
    if gross_after > max_gross:
        violations.append(
            _violation("EXPOSURE_CAP", "gross exposure after the order exceeds the mandate cap")
        )

    pnl_pct = _daily_pnl_pct(portfolio, marks, day_pnl_pct)
    if (
        pnl_pct is not None
        and pnl_pct <= -_decimal(mandate.max_daily_loss_pct)
        and not _is_risk_reducing(order, portfolio)
    ):
        violations.append(
            _violation("DAILY_LOSS_HALT", "daily loss limit reached; new risk is disabled")
        )

    if last_order_ts is not None and order.symbol in last_order_ts:
        elapsed_seconds = _seconds_between(order.created_at, last_order_ts[order.symbol])
        cooldown_seconds = mandate.cooldown_minutes_per_symbol * 60
        if elapsed_seconds < cooldown_seconds:
            violations.append(
                _violation("COOLDOWN", "order is inside the per-symbol cooldown window")
            )

    if orders_today >= mandate.max_orders_per_day and not _is_risk_reducing(order, portfolio):
        violations.append(
            _violation(
                "MAX_ORDERS_PER_DAY",
                f"daily order limit of {mandate.max_orders_per_day} reached",
            )
        )

    if not mandate.allow_short and _would_open_or_increase_short(order, portfolio):
        violations.append(
            _violation("SHORT_NOT_ALLOWED", "order would open or increase a short position")
        )

    if kill_engaged:
        violations.append(_violation("KILL_SWITCH", "kill switch is engaged"))

    result = GateResult(passed=len(violations) == 0, violations=violations)
    append_audit(
        "gate_evaluated",
        {
            "order": order,
            "passed": result.passed,
            "violations": result.violations,
            "orders_today": orders_today,
            "price": price,
            "equity": equity,
            "gross_exposure_after": gross_after,
            "daily_pnl_pct": pnl_pct,
        },
    )
    emit_event(GateEvaluated(run_id=order.run_id or "", result=result))
    return result

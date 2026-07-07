"""Deterministic mandate gate for order safety checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from sentinel.core.events import GateEvaluated
from sentinel.core.models import (
    GateResult,
    Mandate,
    OptionChainSnapshot,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    OptionStrategyProposal,
    Order,
    Portfolio,
    Violation,
    ViolationCode,
)
from sentinel.options.greeks import position_greeks
from sentinel.options.strategies import infer_strategy, max_loss, net_premium
from sentinel.risk._events import emit_event
from sentinel.risk.audit import append_audit
from sentinel.risk.mandate import is_symbol_allowed
from sentinel.risk.sizing import available_cash

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
    unrealized = sum(
        (
            position.qty
            * ((marks or {}).get(position.symbol, position.avg_cost) - position.avg_cost)
            for position in portfolio.positions
        ),
        Decimal("0"),
    )
    return (portfolio.day_pnl + unrealized) / equity * ONE_HUNDRED


def _seconds_between(later: datetime, earlier: datetime) -> float:
    if later.tzinfo is None or earlier.tzinfo is None:
        return (later.replace(tzinfo=None) - earlier.replace(tzinfo=None)).total_seconds()
    return (later - earlier).total_seconds()


def _violation(code: ViolationCode, message: str) -> Violation:
    return Violation(code=code, message=message)


OptionQuoteInput = OptionChainSnapshot | Mapping[str, OptionQuote] | Sequence[OptionQuote] | None


def _option_legs(order_or_proposal: Order | OptionStrategyProposal) -> list[OptionLeg]:
    if isinstance(order_or_proposal, Order):
        return list(order_or_proposal.legs or [])
    return list(order_or_proposal.legs)


def _option_run_id(order_or_proposal: Order | OptionStrategyProposal) -> str:
    return order_or_proposal.run_id or ""


def _option_created_at(order_or_proposal: Order | OptionStrategyProposal) -> datetime:
    return order_or_proposal.created_at


def _option_action(order_or_proposal: Order | OptionStrategyProposal) -> str:
    if isinstance(order_or_proposal, OptionStrategyProposal):
        return order_or_proposal.action
    return "OPEN"


def _underlying_from_legs(legs: Sequence[OptionLeg]) -> str:
    return legs[0].contract.underlying if legs else ""


def _quote_map(chain: OptionQuoteInput) -> dict[str, OptionQuote]:
    if chain is None:
        return {}
    if isinstance(chain, OptionChainSnapshot):
        return {quote.contract.contract_symbol: quote for quote in chain.quotes}
    if isinstance(chain, Mapping):
        return dict(chain)
    return {quote.contract.contract_symbol: quote for quote in chain}


def _as_of_date(value: date | datetime | None, created_at: datetime) -> date:
    if value is None:
        return created_at.date()
    if isinstance(value, datetime):
        return value.date()
    return value


def _dte(leg: OptionLeg, as_of: date) -> int:
    return (leg.contract.expiry - as_of).days


def _trading_days_until(expiry: date, as_of: date) -> int:
    days = 0
    current = as_of
    while current < expiry:
        current = date.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5:
            days += 1
    return days


def _is_option_risk_reducing(
    order_or_proposal: Order | OptionStrategyProposal,
    option_positions: Sequence[OptionPosition],
) -> bool:
    if _option_action(order_or_proposal) == "CLOSE":
        return True
    legs = _option_legs(order_or_proposal)
    if not legs:
        return False
    current: dict[str, int] = {}
    for position in option_positions:
        for leg in position.legs:
            signed = leg.contracts if leg.side == "buy" else -leg.contracts
            current[leg.contract.contract_symbol] = current.get(leg.contract.contract_symbol, 0) + signed
    for leg in legs:
        before = current.get(leg.contract.contract_symbol, 0)
        signed = leg.contracts if leg.side == "buy" else -leg.contracts
        after = before + signed
        if before == 0 or abs(after) >= abs(before):
            return False
    return True


def _stock_shares(portfolio: Portfolio, underlying: str) -> Decimal:
    return _position_qty(portfolio, underlying)


def _stock_downside_notional(portfolio: Portfolio, underlying: str) -> Decimal:
    return sum(
        (
            max(position.qty, Decimal("0")) * position.avg_cost
            for position in portfolio.positions
            if position.symbol == underlying
        ),
        Decimal("0"),
    )


def _combined_legs(
    legs: Sequence[OptionLeg],
    option_positions: Sequence[OptionPosition],
) -> list[OptionLeg]:
    combined: list[OptionLeg] = []
    for position in option_positions:
        combined.extend(position.legs)
    combined.extend(legs)
    return combined


def _signed_contracts_by_key(legs: Sequence[OptionLeg]) -> dict[tuple[str, date, str, Decimal], int]:
    exposures: dict[tuple[str, date, str, Decimal], int] = {}
    for leg in legs:
        key = (
            leg.contract.underlying,
            leg.contract.expiry,
            leg.contract.kind,
            leg.contract.strike,
        )
        signed = leg.contracts if leg.side == "buy" else -leg.contracts
        exposures[key] = exposures.get(key, 0) + signed
    return exposures


def _add_contract_count(
    groups: dict[date, dict[Decimal, int]],
    expiry: date,
    strike: Decimal,
    contracts: int,
) -> None:
    strikes = groups.setdefault(expiry, {})
    strikes[strike] = strikes.get(strike, 0) + contracts


def _remaining_after_protection(
    kind: str,
    shorts_by_expiry: dict[date, dict[Decimal, int]],
    longs_by_expiry: dict[date, dict[Decimal, int]],
) -> dict[tuple[date, Decimal], int]:
    remaining = {expiry: dict(strikes) for expiry, strikes in shorts_by_expiry.items()}
    available_longs = {expiry: dict(strikes) for expiry, strikes in longs_by_expiry.items()}

    for expiry, shorts in remaining.items():
        long_counts = available_longs.get(expiry, {})
        if kind == "call":
            short_strikes = sorted(shorts.keys(), reverse=True)
            long_strikes = sorted(long_counts.keys())
        else:
            short_strikes = sorted(shorts.keys())
            long_strikes = sorted(long_counts.keys(), reverse=True)

        for short_strike in short_strikes:
            needed = shorts[short_strike]
            for long_strike in long_strikes:
                if needed <= 0:
                    break
                if kind == "call" and long_strike <= short_strike:
                    continue
                if kind == "put" and long_strike >= short_strike:
                    continue
                available = long_counts.get(long_strike, 0)
                consumed = min(needed, available)
                needed -= consumed
                long_counts[long_strike] = available - consumed
            shorts[short_strike] = needed

    return {
        (expiry, strike): contracts
        for expiry, strikes in remaining.items()
        for strike, contracts in strikes.items()
        if contracts > 0
    }


def _has_naked_short(
    legs: Sequence[OptionLeg],
    portfolio: Portfolio,
    option_positions: Sequence[OptionPosition],
) -> bool:
    exposures = _signed_contracts_by_key(_combined_legs(legs, option_positions))
    underlyings = {leg.contract.underlying for leg in legs}
    cash_required = Decimal("0")
    for underlying in underlyings:
        call_shorts: dict[date, dict[Decimal, int]] = {}
        call_longs: dict[date, dict[Decimal, int]] = {}
        put_shorts: dict[date, dict[Decimal, int]] = {}
        put_longs: dict[date, dict[Decimal, int]] = {}
        for (u, expiry, kind, strike), signed in exposures.items():
            if u != underlying:
                continue
            contracts = abs(signed)
            if kind == "call" and signed < 0:
                _add_contract_count(call_shorts, expiry, strike, contracts)
            elif kind == "call":
                _add_contract_count(call_longs, expiry, strike, contracts)
            elif signed < 0:
                _add_contract_count(put_shorts, expiry, strike, contracts)
            else:
                _add_contract_count(put_longs, expiry, strike, contracts)

        uncovered_calls = sum(
            _remaining_after_protection("call", call_shorts, call_longs).values()
        )
        covered_calls = int(max(_stock_shares(portfolio, underlying), Decimal("0")) // ONE_HUNDRED)
        if uncovered_calls > covered_calls:
            return True

        uncovered_puts = _remaining_after_protection("put", put_shorts, put_longs)
        cash_required += sum(
            (
                strike * ONE_HUNDRED * Decimal(contracts)
                for (_, strike), contracts in uncovered_puts.items()
            ),
            Decimal("0"),
        )
        if cash_required > portfolio.cash:
            return True
    return False


def _recomputed_max_loss(legs: Sequence[OptionLeg], portfolio: Portfolio) -> Decimal:
    if not legs:
        return Decimal("0")
    stock_notional = _stock_downside_notional(portfolio, legs[0].contract.underlying)
    return max_loss(legs, stock_qty=stock_notional)


def _order_collateral_required(legs: Sequence[OptionLeg]) -> Decimal:
    if not legs:
        return Decimal("0")
    try:
        strategy = infer_strategy(legs)
        premium = net_premium(legs)
    except ValueError:
        return Decimal("0")
    credit = max(-premium, Decimal("0"))
    if strategy == "cash_secured_put":
        leg = legs[0]
        return max(leg.contract.strike * ONE_HUNDRED * Decimal(leg.contracts) - credit, Decimal("0"))
    if strategy in {"bull_put_spread", "bear_call_spread"}:
        strikes = sorted({leg.contract.strike for leg in legs})
        if len(strikes) != 2:
            return Decimal("0")
        contracts = max(leg.contracts for leg in legs)
        return max((strikes[1] - strikes[0]) * ONE_HUNDRED * Decimal(contracts) - credit, Decimal("0"))
    return Decimal("0")


def _sigma_inputs(legs: Sequence[OptionLeg], quotes: Mapping[str, OptionQuote]) -> float | dict[str, float]:
    sigmas: dict[str, float] = {}
    for leg in legs:
        quote = quotes.get(leg.contract.contract_symbol)
        sigma = quote.implied_vol if quote is not None and quote.implied_vol is not None else None
        if sigma is None:
            sigma = quote.model_iv if quote is not None and quote.model_iv is not None else 0.20
        sigmas[leg.contract.contract_symbol] = sigma
    return sigmas


def check_option_order(
    order_or_proposal: Order | OptionStrategyProposal,
    portfolio: Portfolio,
    mandate: Mandate,
    option_positions: Sequence[OptionPosition],
    chain: OptionQuoteInput = None,
    *,
    orders_today: int,
    last_order_ts: dict[str, datetime] | None,
    kill_engaged: bool,
    day_pnl_pct: float | None,
    marks: dict[str, Decimal] | None = None,
    as_of: date | datetime | None = None,
) -> GateResult:
    """Evaluate deterministic option mandate rules and audit the gate result."""

    violations: list[Violation] = []
    legs = _option_legs(order_or_proposal)
    underlying = _underlying_from_legs(legs)
    created_at = _option_created_at(order_or_proposal)
    evaluation_date = _as_of_date(as_of, created_at)
    equity = _equity(portfolio, marks)
    quotes = _quote_map(chain)
    risk_reducing = _is_option_risk_reducing(order_or_proposal, option_positions)

    if not mandate.options.enabled:
        violations.append(_violation("OPTIONS_DISABLED", "options trading is disabled"))

    if underlying not in mandate.options.underlying_universe:
        violations.append(
            _violation("UNDERLYING_NOT_ALLOWED", f"{underlying} is not in the options universe")
        )

    pnl_pct = _daily_pnl_pct(portfolio, marks, day_pnl_pct)
    if (
        pnl_pct is not None
        and pnl_pct <= -_decimal(mandate.max_daily_loss_pct)
        and not risk_reducing
    ):
        violations.append(
            _violation("DAILY_LOSS_HALT", "daily loss limit reached; new option risk is disabled")
        )

    if last_order_ts is not None and underlying in last_order_ts:
        elapsed_seconds = _seconds_between(created_at, last_order_ts[underlying])
        cooldown_seconds = mandate.cooldown_minutes_per_symbol * 60
        if elapsed_seconds < cooldown_seconds:
            violations.append(
                _violation("COOLDOWN", "option order is inside the underlying cooldown window")
            )

    if orders_today >= mandate.options.max_option_orders_per_day and not risk_reducing:
        violations.append(
            _violation(
                "MAX_ORDERS_PER_DAY",
                f"daily option order limit of {mandate.options.max_option_orders_per_day} reached",
            )
        )

    if kill_engaged:
        violations.append(_violation("KILL_SWITCH", "kill switch is engaged"))

    if any(leg.contracts > mandate.options.max_contracts_per_order for leg in legs):
        violations.append(_violation("ORDER_TOO_LARGE", "option contract count exceeds mandate cap"))

    order_max_loss: Decimal
    try:
        order_max_loss = _recomputed_max_loss(legs, portfolio)
    except ValueError as exc:
        order_max_loss = Decimal("Infinity")
        violations.append(_violation("MAX_LOSS_EXCEEDED", f"option max loss is unsupported: {exc}"))

    if order_max_loss > _decimal(mandate.options.max_loss_per_position_usd):
        violations.append(
            _violation("MAX_LOSS_EXCEEDED", "option max loss exceeds per-position cap")
        )

    total_open_max_loss = sum((position.max_loss for position in option_positions), Decimal("0"))
    budget_cap = _pct_amount(max(equity, Decimal("0")), mandate.options.max_total_options_max_loss_pct_equity)
    if total_open_max_loss + order_max_loss > budget_cap:
        violations.append(
            _violation("OPTIONS_BUDGET_EXCEEDED", "total options max loss exceeds mandate budget")
        )

    if _has_naked_short(legs, portfolio, option_positions):
        violations.append(_violation("NAKED_SHORT_OPTION", "option order creates naked short exposure"))

    collateral_required = _order_collateral_required(legs)
    cash_available = available_cash(portfolio, option_positions)
    if collateral_required > cash_available:
        violations.append(
            _violation("INSUFFICIENT_COLLATERAL", "available cash is insufficient for option collateral")
        )

    for leg in legs:
        quote = quotes.get(leg.contract.contract_symbol)
        if quote is None:
            violations.append(_violation("ILLIQUID_CONTRACT", "missing option quote"))
            continue
        mid = (quote.bid + quote.ask) / Decimal("2")
        rel_spread = Decimal("Infinity") if mid <= 0 else (quote.ask - quote.bid) / mid * ONE_HUNDRED
        if (
            quote.open_interest < mandate.options.min_open_interest
            or quote.bid < 0
            or quote.ask <= quote.bid
            or rel_spread > _decimal(mandate.options.max_rel_spread_pct)
        ):
            violations.append(
                _violation("ILLIQUID_CONTRACT", "option quote fails open-interest or spread limits")
            )
            break

        dte = _dte(leg, evaluation_date)
        if dte < mandate.options.min_dte or dte > mandate.options.max_dte:
            violations.append(_violation("DTE_OUT_OF_RANGE", "option expiry is outside DTE limits"))
            break

        if _trading_days_until(leg.contract.expiry, evaluation_date) <= 2:
            violations.append(_violation("EXPIRY_TOO_CLOSE", "option expiry is too close"))
            break

    try:
        combined = _combined_legs(legs, option_positions)
        spot = float((marks or {}).get(underlying, Decimal("0")))
        if spot <= 0 and isinstance(chain, OptionChainSnapshot):
            spot = float(chain.spot)
        if spot > 0 and combined:
            greeks = position_greeks(
                combined,
                spot,
                chain.risk_free_rate if isinstance(chain, OptionChainSnapshot) else 0.0,
                chain.dividend_yield if isinstance(chain, OptionChainSnapshot) else 0.0,
                _sigma_inputs(combined, quotes),
                as_of=evaluation_date,
            )
            if (
                abs(greeks.delta) > mandate.options.max_net_portfolio_delta_abs
                or abs(greeks.vega) > mandate.options.max_net_portfolio_vega_abs
            ):
                violations.append(
                    _violation("GREEKS_CAP_EXCEEDED", "net option delta or vega exceeds mandate cap")
                )
    except (KeyError, ValueError):
        violations.append(_violation("GREEKS_CAP_EXCEEDED", "unable to compute option Greeks"))

    result = GateResult(passed=len(violations) == 0, violations=violations)
    append_audit(
        "option_gate_evaluated",
        {
            "order": order_or_proposal,
            "legs": legs,
            "underlying": underlying,
            "passed": result.passed,
            "violations": result.violations,
            "orders_today": orders_today,
            "equity": equity,
            "daily_pnl_pct": pnl_pct,
            "max_loss": order_max_loss,
            "available_cash": cash_available,
            "collateral_required": collateral_required,
        },
    )
    emit_event(GateEvaluated(run_id=_option_run_id(order_or_proposal), result=result))
    return result


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

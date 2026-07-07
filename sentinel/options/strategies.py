"""Deterministic option strategy builders and closed-form economics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from sentinel.core.models import OptionContract, OptionLeg, OptionQuote, OptionStrategyId

MULTIPLIER = Decimal("100")
_ZERO = Decimal("0")

QuoteLike = OptionQuote | Decimal | int | float | str
QuoteMap = Mapping[str | OptionContract, QuoteLike]


def _d(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _leg(
    contract: OptionContract,
    side: str,
    contracts: int = 1,
    limit_price: Decimal | None = None,
) -> OptionLeg:
    if contracts < 1:
        raise ValueError("contracts must be >= 1")
    return OptionLeg(
        contract=contract,
        side=side,  # type: ignore[arg-type]
        contracts=contracts,
        limit_price=limit_price,
    )


def build_long_call(call: OptionContract, contracts: int = 1, limit_price: Decimal | None = None) -> list[OptionLeg]:
    _require_kind(call, "call")
    return [_leg(call, "buy", contracts, limit_price)]


def build_long_put(put: OptionContract, contracts: int = 1, limit_price: Decimal | None = None) -> list[OptionLeg]:
    _require_kind(put, "put")
    return [_leg(put, "buy", contracts, limit_price)]


def build_covered_call(call: OptionContract, contracts: int = 1, limit_price: Decimal | None = None) -> list[OptionLeg]:
    _require_kind(call, "call")
    return [_leg(call, "sell", contracts, limit_price)]


def build_cash_secured_put(put: OptionContract, contracts: int = 1, limit_price: Decimal | None = None) -> list[OptionLeg]:
    _require_kind(put, "put")
    return [_leg(put, "sell", contracts, limit_price)]


def build_bull_call_spread(
    long_call: OptionContract,
    short_call: OptionContract,
    contracts: int = 1,
    long_price: Decimal | None = None,
    short_price: Decimal | None = None,
) -> list[OptionLeg]:
    _require_kind(long_call, "call")
    _require_kind(short_call, "call")
    _require_same_expiry(long_call, short_call)
    if not long_call.strike < short_call.strike:
        raise ValueError("bull_call_spread requires long strike < short strike")
    return [_leg(long_call, "buy", contracts, long_price), _leg(short_call, "sell", contracts, short_price)]


def build_bear_put_spread(
    long_put: OptionContract,
    short_put: OptionContract,
    contracts: int = 1,
    long_price: Decimal | None = None,
    short_price: Decimal | None = None,
) -> list[OptionLeg]:
    _require_kind(long_put, "put")
    _require_kind(short_put, "put")
    _require_same_expiry(long_put, short_put)
    if not short_put.strike < long_put.strike:
        raise ValueError("bear_put_spread requires short strike < long strike")
    return [_leg(long_put, "buy", contracts, long_price), _leg(short_put, "sell", contracts, short_price)]


def build_bull_put_spread(
    short_put: OptionContract,
    long_put: OptionContract,
    contracts: int = 1,
    short_price: Decimal | None = None,
    long_price: Decimal | None = None,
) -> list[OptionLeg]:
    _require_kind(short_put, "put")
    _require_kind(long_put, "put")
    _require_same_expiry(short_put, long_put)
    if not long_put.strike < short_put.strike:
        raise ValueError("bull_put_spread requires long strike < short strike")
    return [_leg(short_put, "sell", contracts, short_price), _leg(long_put, "buy", contracts, long_price)]


def build_bear_call_spread(
    short_call: OptionContract,
    long_call: OptionContract,
    contracts: int = 1,
    short_price: Decimal | None = None,
    long_price: Decimal | None = None,
) -> list[OptionLeg]:
    _require_kind(short_call, "call")
    _require_kind(long_call, "call")
    _require_same_expiry(short_call, long_call)
    if not short_call.strike < long_call.strike:
        raise ValueError("bear_call_spread requires short strike < long strike")
    return [_leg(short_call, "sell", contracts, short_price), _leg(long_call, "buy", contracts, long_price)]


def build_long_straddle(
    call: OptionContract,
    put: OptionContract,
    contracts: int = 1,
    call_price: Decimal | None = None,
    put_price: Decimal | None = None,
) -> list[OptionLeg]:
    _require_kind(call, "call")
    _require_kind(put, "put")
    _require_same_expiry(call, put)
    if call.strike != put.strike:
        raise ValueError("long_straddle requires same strike")
    return [_leg(call, "buy", contracts, call_price), _leg(put, "buy", contracts, put_price)]


def build_long_strangle(
    put: OptionContract,
    call: OptionContract,
    contracts: int = 1,
    put_price: Decimal | None = None,
    call_price: Decimal | None = None,
) -> list[OptionLeg]:
    _require_kind(put, "put")
    _require_kind(call, "call")
    _require_same_expiry(put, call)
    if not put.strike < call.strike:
        raise ValueError("long_strangle requires put strike < call strike")
    return [_leg(put, "buy", contracts, put_price), _leg(call, "buy", contracts, call_price)]


def net_premium(legs: Sequence[OptionLeg], quotes: QuoteMap | None = None) -> Decimal:
    """Return signed entry premium in dollars: debit > 0, credit < 0."""
    total = _ZERO
    for leg in legs:
        price = _price_for_leg(leg, quotes)
        sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
        total += sign * price * _d(leg.contracts) * _d(leg.contract.multiplier)
    return total


def max_loss(legs: Sequence[OptionLeg], stock_qty: Decimal | int | float = 0) -> Decimal:
    strategy = infer_strategy(legs)
    premium = net_premium(legs)
    contracts = _contracts(legs)
    width = _width(legs)
    if strategy in {"long_call", "long_put", "bull_call_spread", "bear_put_spread", "long_straddle", "long_strangle"}:
        return max(premium, _ZERO)
    if strategy in {"bull_put_spread", "bear_call_spread"}:
        return width * MULTIPLIER * _d(contracts) - (-premium)
    if strategy == "cash_secured_put":
        strike = legs[0].contract.strike
        return strike * MULTIPLIER * _d(contracts) - (-premium)
    if strategy == "covered_call":
        stock_notional_at_risk = _d(stock_qty)
        if stock_notional_at_risk <= 0:
            raise ValueError("covered_call max_loss requires stock downside notional in stock_qty")
        return stock_notional_at_risk - (-premium)
    raise ValueError(f"unsupported strategy: {strategy}")


def max_gain(legs: Sequence[OptionLeg]) -> Decimal | None:
    strategy = infer_strategy(legs)
    premium = net_premium(legs)
    contracts = _contracts(legs)
    width = _width(legs)
    if strategy in {"long_call", "long_put", "covered_call", "long_straddle", "long_strangle"}:
        return None
    if strategy in {"bull_call_spread", "bear_put_spread"}:
        return width * MULTIPLIER * _d(contracts) - premium
    if strategy in {"bull_put_spread", "bear_call_spread", "cash_secured_put"}:
        return -premium
    raise ValueError(f"unsupported strategy: {strategy}")


def breakevens(legs: Sequence[OptionLeg]) -> list[Decimal]:
    strategy = infer_strategy(legs)
    premium = net_premium(legs)
    contracts = _d(_contracts(legs))
    denom = MULTIPLIER * contracts
    if strategy == "long_call":
        return [legs[0].contract.strike + premium / denom]
    if strategy == "long_put":
        return [legs[0].contract.strike - premium / denom]
    if strategy == "cash_secured_put":
        return [legs[0].contract.strike - (-premium) / denom]
    if strategy == "covered_call":
        return []
    if strategy == "bull_call_spread":
        long_leg = _by_side(legs, "buy")[0]
        return [long_leg.contract.strike + premium / denom]
    if strategy == "bear_put_spread":
        long_leg = _by_side(legs, "buy")[0]
        return [long_leg.contract.strike - premium / denom]
    if strategy == "bull_put_spread":
        short_leg = _by_side(legs, "sell")[0]
        return [short_leg.contract.strike - (-premium) / denom]
    if strategy == "bear_call_spread":
        short_leg = _by_side(legs, "sell")[0]
        return [short_leg.contract.strike + (-premium) / denom]
    if strategy == "long_straddle":
        strike = legs[0].contract.strike
        move = premium / denom
        return [strike - move, strike + move]
    if strategy == "long_strangle":
        put_leg = _kind_legs(legs, "put")[0]
        call_leg = _kind_legs(legs, "call")[0]
        move = premium / denom
        return [put_leg.contract.strike - move, call_leg.contract.strike + move]
    raise ValueError(f"unsupported strategy: {strategy}")


def payoff_at_expiry(legs: Sequence[OptionLeg], S_T: Decimal | int | float | str) -> Decimal:
    """Return expiry P/L including entry premium."""
    spot = _d(S_T)
    total = -net_premium(legs)
    for leg in legs:
        strike = leg.contract.strike
        if leg.contract.kind == "call":
            intrinsic = max(spot - strike, _ZERO)
        else:
            intrinsic = max(strike - spot, _ZERO)
        sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
        total += sign * intrinsic * _d(leg.contracts) * _d(leg.contract.multiplier)
    return total


def infer_strategy(legs: Sequence[OptionLeg]) -> OptionStrategyId:
    if len(legs) == 1:
        leg = legs[0]
        if leg.contract.kind == "call" and leg.side == "buy":
            return "long_call"
        if leg.contract.kind == "put" and leg.side == "buy":
            return "long_put"
        if leg.contract.kind == "call" and leg.side == "sell":
            return "covered_call"
        if leg.contract.kind == "put" and leg.side == "sell":
            return "cash_secured_put"
    if len(legs) == 2:
        calls = _kind_legs(legs, "call")
        puts = _kind_legs(legs, "put")
        buys = _by_side(legs, "buy")
        sells = _by_side(legs, "sell")
        if len(calls) == 2 and len(buys) == 1 and len(sells) == 1:
            buy, sell = buys[0], sells[0]
            return "bull_call_spread" if buy.contract.strike < sell.contract.strike else "bear_call_spread"
        if len(puts) == 2 and len(buys) == 1 and len(sells) == 1:
            buy, sell = buys[0], sells[0]
            return "bear_put_spread" if sell.contract.strike < buy.contract.strike else "bull_put_spread"
        if len(calls) == 1 and len(puts) == 1 and len(buys) == 2:
            return "long_straddle" if calls[0].contract.strike == puts[0].contract.strike else "long_strangle"
    raise ValueError("legs do not match a supported v1 option strategy")


def _price_for_leg(leg: OptionLeg, quotes: QuoteMap | None) -> Decimal:
    if quotes is not None:
        quote = quotes.get(leg.contract.contract_symbol)
        if quote is None:
            try:
                quote = quotes.get(leg.contract)
            except TypeError:
                quote = None
        if quote is not None:
            if isinstance(quote, OptionQuote):
                return quote.ask if leg.side == "buy" else quote.bid
            return _d(quote)
    if leg.limit_price is None:
        raise ValueError(f"missing limit_price or quote for {leg.contract.contract_symbol}")
    return leg.limit_price


def _contracts(legs: Sequence[OptionLeg]) -> int:
    values = {leg.contracts for leg in legs}
    if len(values) != 1:
        raise ValueError("closed-form economics require equal contract counts")
    return values.pop()


def _width(legs: Sequence[OptionLeg]) -> Decimal:
    strikes = sorted({leg.contract.strike for leg in legs})
    if len(strikes) != 2:
        return _ZERO
    return strikes[1] - strikes[0]


def _by_side(legs: Sequence[OptionLeg], side: str) -> list[OptionLeg]:
    return [leg for leg in legs if leg.side == side]


def _kind_legs(legs: Sequence[OptionLeg], kind: str) -> list[OptionLeg]:
    return [leg for leg in legs if leg.contract.kind == kind]


def _require_kind(contract: OptionContract, kind: str) -> None:
    if contract.kind != kind:
        raise ValueError(f"contract must be a {kind}")


def _require_same_expiry(first: OptionContract, second: OptionContract) -> None:
    if first.expiry != second.expiry or first.underlying != second.underlying:
        raise ValueError("strategy legs must share underlying and expiry")

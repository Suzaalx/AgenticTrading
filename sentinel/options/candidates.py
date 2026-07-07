"""Deterministic option-strategy candidate builder."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sentinel.core.models import (
    OptionChainSnapshot,
    OptionLeg,
    OptionQuote,
    OptionStrategyCandidate,
)
from sentinel.options.greeks import position_greeks
from sentinel.options.pricing import D_CLAMP, norm_cdf
from sentinel.options.strategies import (
    breakevens,
    build_bear_call_spread,
    build_bear_put_spread,
    build_bull_call_spread,
    build_bull_put_spread,
    build_cash_secured_put,
    build_long_call,
    build_long_put,
    build_long_straddle,
    build_long_strangle,
    max_gain,
    max_loss,
    net_premium,
)

DEFAULT_MIN_DTE = 21
DEFAULT_MAX_DTE = 60
DEFAULT_MIN_OI = 100
DEFAULT_MAX_SPREAD_PCT = 10.0
CONTRACT_MULTIPLIER = Decimal("100")


def build_candidates(
    chain: OptionChainSnapshot,
    direction: str,
    conviction: int | float,
    iv_context: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | object | None = None,
) -> list[OptionStrategyCandidate]:
    """Build 3-6 liquid, defined-risk option candidates with pre-computed economics.

    The probability proxy uses BSM risk-neutral terminal probabilities: calls use
    ``1 - N(d2(K))``, puts use ``N(-d2(K))``, and ranges use the probability of finishing
    outside/inside the relevant breakevens under the lognormal distribution.
    """

    iv_ctx = iv_context or {}
    expiry = _select_expiry(chain, settings)
    if expiry is None:
        return []
    quotes = [_with_entry_price(q) for q in _liquid_quotes(chain, expiry, settings)]
    if not quotes:
        return []

    iv_rank = _float_setting(iv_ctx, "iv_rank", chain.iv_rank if chain.iv_rank is not None else 0.0)
    event_within_dte = bool(iv_ctx.get("event_within_dte", iv_ctx.get("has_event", False)))
    normalized_direction = direction.lower()
    plans = _plans(normalized_direction, iv_rank, event_within_dte)

    candidates: list[OptionStrategyCandidate] = []
    for strategy in plans:
        legs = _build_strategy_legs(strategy, quotes)
        if legs is None:
            continue
        candidate = _candidate(chain, strategy, legs, normalized_direction, conviction, iv_rank)
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= 6:
            break
    return candidates[:6]


def _select_expiry(chain: OptionChainSnapshot, settings: Mapping[str, Any] | object | None) -> date | None:
    min_dte = int(_setting(settings, "min_dte", DEFAULT_MIN_DTE))
    max_dte = int(_setting(settings, "max_dte", DEFAULT_MAX_DTE))
    today = chain.as_of.date()
    eligible = [expiry for expiry in chain.expiries if min_dte <= (expiry - today).days <= max_dte]
    monthly = [expiry for expiry in eligible if _is_monthly_expiry(expiry)]
    return min(monthly or eligible, default=None)


def _is_monthly_expiry(expiry: date) -> bool:
    return expiry.weekday() == 4 and 15 <= expiry.day <= 21


def _liquid_quotes(
    chain: OptionChainSnapshot,
    expiry: date,
    settings: Mapping[str, Any] | object | None,
) -> list[OptionQuote]:
    min_oi = int(_setting(settings, "min_oi", _setting(settings, "min_open_interest", DEFAULT_MIN_OI)))
    max_spread_pct = float(
        _setting(settings, "max_spread_pct", _setting(settings, "max_rel_spread_pct", DEFAULT_MAX_SPREAD_PCT))
    )
    max_spread_ratio = max_spread_pct / 100.0 if max_spread_pct > 1.0 else max_spread_pct
    result: list[OptionQuote] = []
    for quote in chain.quotes:
        if quote.contract.expiry != expiry or quote.open_interest < min_oi or quote.bid <= 0:
            continue
        mid = (quote.bid + quote.ask) / Decimal("2")
        if mid <= 0:
            continue
        spread_ratio = float((quote.ask - quote.bid) / mid)
        if spread_ratio <= max_spread_ratio:
            result.append(quote)
    return result


def _with_entry_price(quote: OptionQuote) -> OptionQuote:
    return quote


def _plans(direction: str, iv_rank: float, event_within_dte: bool) -> list[str]:
    high_iv = iv_rank > 0.5
    if direction == "neutral":
        if event_within_dte or not high_iv:
            return ["long_straddle", "long_strangle"]
        return ["long_strangle", "long_straddle"]
    if direction == "bullish":
        if high_iv:
            return ["bull_put_spread", "cash_secured_put", "bull_call_spread", "long_call"]
        return ["long_call", "bull_call_spread", "bull_put_spread"]
    if direction == "bearish":
        if high_iv:
            return ["bear_call_spread", "bear_put_spread", "long_put"]
        return ["long_put", "bear_put_spread", "bear_call_spread"]
    return []


def _build_strategy_legs(strategy: str, quotes: Sequence[OptionQuote]) -> list[OptionLeg] | None:
    calls = [q for q in quotes if q.contract.kind == "call"]
    puts = [q for q in quotes if q.contract.kind == "put"]
    if strategy == "long_call":
        quote = _by_delta(calls, 0.60)
        return None if quote is None else build_long_call(quote.contract, limit_price=quote.ask)
    if strategy == "long_put":
        quote = _by_delta(puts, -0.60)
        return None if quote is None else build_long_put(quote.contract, limit_price=quote.ask)
    if strategy == "cash_secured_put":
        quote = _by_delta(puts, -0.30)
        return None if quote is None else build_cash_secured_put(quote.contract, limit_price=quote.bid)
    if strategy == "bull_call_spread":
        long_q = _by_delta(calls, 0.60)
        short_q = _by_delta(calls, 0.30)
        if long_q is None or short_q is None or not long_q.contract.strike < short_q.contract.strike:
            return None
        return build_bull_call_spread(long_q.contract, short_q.contract, long_price=long_q.ask, short_price=short_q.bid)
    if strategy == "bear_put_spread":
        long_q = _by_delta(puts, -0.60)
        short_q = _by_delta(puts, -0.30)
        if long_q is None or short_q is None or not short_q.contract.strike < long_q.contract.strike:
            return None
        return build_bear_put_spread(long_q.contract, short_q.contract, long_price=long_q.ask, short_price=short_q.bid)
    if strategy == "bull_put_spread":
        short_q = _by_delta(puts, -0.30)
        long_q = _nearest_lower_strike(puts, short_q)
        if short_q is None or long_q is None:
            return None
        return build_bull_put_spread(short_q.contract, long_q.contract, short_price=short_q.bid, long_price=long_q.ask)
    if strategy == "bear_call_spread":
        short_q = _by_delta(calls, 0.30)
        long_q = _nearest_higher_strike(calls, short_q)
        if short_q is None or long_q is None:
            return None
        return build_bear_call_spread(short_q.contract, long_q.contract, short_price=short_q.bid, long_price=long_q.ask)
    if strategy == "long_straddle":
        call_q, put_q = _atm_pair(calls, puts)
        if call_q is None or put_q is None:
            return None
        return build_long_straddle(call_q.contract, put_q.contract, call_price=call_q.ask, put_price=put_q.ask)
    if strategy == "long_strangle":
        call_q = _by_delta(calls, 0.25)
        put_q = _by_delta(puts, -0.25)
        if call_q is None or put_q is None or not put_q.contract.strike < call_q.contract.strike:
            return None
        return build_long_strangle(put_q.contract, call_q.contract, put_price=put_q.ask, call_price=call_q.ask)
    return None


def _candidate(
    chain: OptionChainSnapshot,
    strategy: str,
    legs: list[OptionLeg],
    direction: str,
    conviction: int | float,
    iv_rank: float,
) -> OptionStrategyCandidate | None:
    try:
        premium = net_premium(legs)
        loss = max_loss(legs, stock_qty=chain.spot * CONTRACT_MULTIPLIER)
        gain = max_gain(legs)
        be = breakevens(legs)
    except ValueError:
        return None
    sigmas = {
        leg.contract.contract_symbol: _leg_sigma(leg, chain.atm_iv if chain.atm_iv is not None else 0.30)
        for leg in legs
    }
    greeks = position_greeks(
        legs,
        float(chain.spot),
        chain.risk_free_rate,
        chain.dividend_yield,
        sigmas,
        as_of=chain.as_of,
    )
    return OptionStrategyCandidate(
        strategy=strategy,  # type: ignore[arg-type]
        legs=legs,
        net_premium=premium,
        max_loss=loss,
        max_gain=gain,
        breakevens=be,
        est_pop=_est_pop(chain, legs[0].contract.expiry, be, strategy, sigmas),
        net_delta=greeks.delta,
        net_vega=greeks.vega,
        net_theta=greeks.theta,
        liquidity_score=_liquidity_score(legs, chain.quotes),
        rationale_facts=(
            f"{direction} {strategy}; conviction={conviction}; iv_rank={iv_rank:.2f}; "
            f"all legs pass OI/bid/spread filters"
        ),
    )


def _est_pop(
    chain: OptionChainSnapshot,
    expiry: date,
    breakeven_values: Sequence[Decimal],
    strategy: str,
    sigmas: Mapping[str, float],
) -> float | None:
    if not breakeven_values:
        return None
    sigma = sum(sigmas.values()) / len(sigmas)
    T = max((expiry - chain.as_of.date()).days / 365.0, 0.0)
    if T <= 0.0 or sigma <= 0.0:
        return None
    spot = float(chain.spot)
    r = chain.risk_free_rate
    q = chain.dividend_yield
    if len(breakeven_values) == 1:
        be = float(breakeven_values[0])
        prob_above = 1.0 - _terminal_cdf(spot, be, T, r, q, sigma)
        if strategy in {"long_call", "bull_call_spread", "bull_put_spread", "cash_secured_put"}:
            return prob_above
        return 1.0 - prob_above
    low, high = sorted(float(value) for value in breakeven_values[:2])
    prob_below = _terminal_cdf(spot, low, T, r, q, sigma)
    prob_above = 1.0 - _terminal_cdf(spot, high, T, r, q, sigma)
    if strategy in {"long_straddle", "long_strangle"}:
        return prob_below + prob_above
    return max(0.0, 1.0 - prob_below - prob_above)


def _terminal_cdf(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    sqrt_t = math.sqrt(T)
    z = (math.log(K / S) - (r - q - 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    return norm_cdf(max(-D_CLAMP, min(D_CLAMP, z)))


def _by_delta(quotes: Sequence[OptionQuote], target: float) -> OptionQuote | None:
    with_delta = [q for q in quotes if q.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda q: _delta_distance(q, target))
    return _middle_quote(quotes)


def _delta_distance(quote: OptionQuote, target: float) -> float:
    if quote.delta is None:
        return math.inf
    return abs(float(quote.delta) - target)


def _middle_quote(quotes: Sequence[OptionQuote]) -> OptionQuote | None:
    if not quotes:
        return None
    sorted_quotes = sorted(quotes, key=lambda q: q.contract.strike)
    return sorted_quotes[len(sorted_quotes) // 2]


def _nearest_lower_strike(quotes: Sequence[OptionQuote], reference: OptionQuote | None) -> OptionQuote | None:
    if reference is None:
        return None
    lower = [q for q in quotes if q.contract.strike < reference.contract.strike]
    return max(lower, key=lambda q: q.contract.strike, default=None)


def _nearest_higher_strike(quotes: Sequence[OptionQuote], reference: OptionQuote | None) -> OptionQuote | None:
    if reference is None:
        return None
    higher = [q for q in quotes if q.contract.strike > reference.contract.strike]
    return min(higher, key=lambda q: q.contract.strike, default=None)


def _atm_pair(calls: Sequence[OptionQuote], puts: Sequence[OptionQuote]) -> tuple[OptionQuote | None, OptionQuote | None]:
    common = {q.contract.strike for q in calls} & {q.contract.strike for q in puts}
    if not common:
        return None, None
    strike = min(common, key=lambda value: abs(value - _median_strike([*calls, *puts])))
    call = next(q for q in calls if q.contract.strike == strike)
    put = next(q for q in puts if q.contract.strike == strike)
    return call, put


def _median_strike(quotes: Sequence[OptionQuote]) -> Decimal:
    strikes = sorted(q.contract.strike for q in quotes)
    return strikes[len(strikes) // 2]


def _leg_sigma(leg: OptionLeg, default: float) -> float:
    if default > 0.0:
        return default
    _ = leg
    return 0.30


def _liquidity_score(legs: Sequence[OptionLeg], quotes: Sequence[OptionQuote]) -> float:
    by_symbol = {quote.contract.contract_symbol: quote for quote in quotes}
    scores: list[float] = []
    for leg in legs:
        quote = by_symbol.get(leg.contract.contract_symbol)
        if quote is None:
            continue
        mid = (quote.bid + quote.ask) / Decimal("2")
        spread_ratio = float((quote.ask - quote.bid) / mid) if mid > 0 else 1.0
        oi_score = min(1.0, quote.open_interest / 1000.0)
        spread_score = max(0.0, 1.0 - spread_ratio / 0.10)
        scores.append(0.5 * oi_score + 0.5 * spread_score)
    return sum(scores) / len(scores) if scores else 0.0


def _setting(settings: Mapping[str, Any] | object | None, name: str, default: Any) -> Any:
    if settings is None:
        return default
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _float_setting(settings: Mapping[str, Any], name: str, default: float) -> float:
    value = settings.get(name, default)
    return float(default if value is None else value)

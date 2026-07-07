"""Closed-form scalar Black-Scholes-Merton Greeks."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sentinel.core.models import OptionLeg
from sentinel.options.pricing import D_CLAMP, norm_cdf

INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def phi(x: float) -> float:
    """Return the standard-normal probability density φ(x)."""
    clamped = max(-D_CLAMP, min(D_CLAMP, x))
    return INV_SQRT_2PI * math.exp(-0.5 * clamped * clamped)


def _validate_kind(kind: str) -> str:
    normalized = kind.lower()
    if normalized not in {"call", "put"}:
        raise ValueError("kind must be 'call' or 'put'")
    return normalized


def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d1 = max(-D_CLAMP, min(D_CLAMP, d1))
    d2 = max(-D_CLAMP, min(D_CLAMP, d1 - sigma * sqrt_t))
    return d1, d2


def delta(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> float:
    """Return BSM delta for one option contract multiplier share."""
    option_kind = _validate_kind(kind)
    if T <= 0.0 or sigma <= 0.0:
        if S == K:
            return 0.5 if option_kind == "call" else -0.5
        if option_kind == "call":
            return math.exp(-q * max(T, 0.0)) if S > K else 0.0
        return -math.exp(-q * max(T, 0.0)) if S < K else 0.0
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    if option_kind == "call":
        return math.exp(-q * T) * norm_cdf(d1)
    return math.exp(-q * T) * (norm_cdf(d1) - 1.0)


def gamma(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Return BSM gamma per underlying-price unit."""
    if T <= 0.0 or sigma <= 0.0 or S <= 0.0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    return math.exp(-q * T) * phi(d1) / (S * sigma * math.sqrt(T))


def vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Return BSM vega per 1.00 volatility; divide by 100 for one vol point."""
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    return S * math.exp(-q * T) * phi(d1) * math.sqrt(T)


def theta(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> float:
    """Return BSM theta per year; divide by 365 for calendar-day theta."""
    option_kind = _validate_kind(kind)
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    first = -S * math.exp(-q * T) * phi(d1) * sigma / (2.0 * math.sqrt(T))
    if option_kind == "call":
        return first - r * K * math.exp(-r * T) * norm_cdf(d2) + q * S * math.exp(-q * T) * norm_cdf(d1)
    return first + r * K * math.exp(-r * T) * norm_cdf(-d2) - q * S * math.exp(-q * T) * norm_cdf(-d1)


def rho(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> float:
    """Return BSM rho per 1.00 rate; divide by 100 for one rate point."""
    option_kind = _validate_kind(kind)
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    _, d2 = _d1_d2(S, K, T, r, q, sigma)
    if option_kind == "call":
        return K * T * math.exp(-r * T) * norm_cdf(d2)
    return -K * T * math.exp(-r * T) * norm_cdf(-d2)


# position_greeks added by WS1b


@dataclass(frozen=True)
class PositionGreeks:
    """Net option-position Greeks in contract-multiplied share units."""

    delta: float
    gamma: float
    vega: float
    theta: float


def position_greeks(
    legs: Sequence[OptionLeg],
    S: float,
    r: float,
    q: float,
    sigma_by_leg_or_scalar: float | Mapping[str, float],
    T: float | None = None,
    as_of: date | datetime | None = None,
) -> PositionGreeks:
    """Return signed net delta/gamma/vega/theta for option legs.

    Scalar Greeks are multiplied by buy/sell sign, contracts, and contract multiplier.
    Vega is per 1.00 volatility and theta is per year, matching the scalar helpers.
    """

    total_delta = 0.0
    total_gamma = 0.0
    total_vega = 0.0
    total_theta = 0.0
    for leg in legs:
        sigma = _sigma_for_leg(leg, sigma_by_leg_or_scalar)
        leg_t = T if T is not None else _time_to_expiry(leg, as_of)
        strike = float(leg.contract.strike)
        sign = 1.0 if leg.side == "buy" else -1.0
        scale = sign * float(leg.contracts) * float(leg.contract.multiplier)
        total_delta += scale * delta(S, strike, leg_t, r, q, sigma, leg.contract.kind)
        total_gamma += scale * gamma(S, strike, leg_t, r, q, sigma)
        total_vega += scale * vega(S, strike, leg_t, r, q, sigma)
        total_theta += scale * theta(S, strike, leg_t, r, q, sigma, leg.contract.kind)
    return PositionGreeks(
        delta=total_delta,
        gamma=total_gamma,
        vega=total_vega,
        theta=total_theta,
    )


def sum_position_greeks(values: Iterable[PositionGreeks]) -> PositionGreeks:
    """Sum already-computed position Greeks into portfolio-level Greeks."""

    total_delta = 0.0
    total_gamma = 0.0
    total_vega = 0.0
    total_theta = 0.0
    for value in values:
        total_delta += value.delta
        total_gamma += value.gamma
        total_vega += value.vega
        total_theta += value.theta
    return PositionGreeks(total_delta, total_gamma, total_vega, total_theta)


def portfolio_greeks(
    positions: Iterable[Sequence[OptionLeg]],
    S: float,
    r: float,
    q: float,
    sigma_by_leg_or_scalar: float | Mapping[str, float],
    T: float | None = None,
    as_of: date | datetime | None = None,
) -> PositionGreeks:
    """Compute portfolio-level Greeks by summing position-level Greeks."""

    return sum_position_greeks(
        position_greeks(position, S, r, q, sigma_by_leg_or_scalar, T=T, as_of=as_of)
        for position in positions
    )


def _sigma_for_leg(leg: OptionLeg, sigma_by_leg_or_scalar: float | Mapping[str, float]) -> float:
    if isinstance(sigma_by_leg_or_scalar, Mapping):
        symbol = leg.contract.contract_symbol
        if symbol not in sigma_by_leg_or_scalar:
            raise KeyError(f"missing sigma for {symbol}")
        return sigma_by_leg_or_scalar[symbol]
    return sigma_by_leg_or_scalar


def _time_to_expiry(leg: OptionLeg, as_of: date | datetime | None) -> float:
    if as_of is None:
        current = date.today()
    elif isinstance(as_of, datetime):
        current = as_of.date()
    else:
        current = as_of
    return max((leg.contract.expiry - current).days, 0) / 365.0

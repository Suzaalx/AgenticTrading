"""Black-Scholes-Merton and Cox-Ross-Rubinstein option pricing."""

from __future__ import annotations

import math

SQRT_2 = math.sqrt(2.0)
D_CLAMP = 40.0


def norm_cdf(x: float) -> float:
    """Return the standard-normal cumulative distribution N(x)."""
    clamped = max(-D_CLAMP, min(D_CLAMP, x))
    return 0.5 * (1.0 + math.erf(clamped / SQRT_2))


def _validate_kind(kind: str) -> str:
    normalized = kind.lower()
    if normalized not in {"call", "put"}:
        raise ValueError("kind must be 'call' or 'put'")
    return normalized


def intrinsic(S: float, K: float, kind: str) -> float:
    """Return option intrinsic value at spot S."""
    option_kind = _validate_kind(kind)
    if option_kind == "call":
        return max(S - K, 0.0)
    return max(K - S, 0.0)


def _discounted_forward_intrinsic(S: float, K: float, T: float, r: float, q: float, kind: str) -> float:
    discounted_spot = S * math.exp(-q * T)
    discounted_strike = K * math.exp(-r * T)
    if _validate_kind(kind) == "call":
        return max(discounted_spot - discounted_strike, 0.0)
    return max(discounted_strike - discounted_spot, 0.0)


def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d1 = max(-D_CLAMP, min(D_CLAMP, d1))
    d2 = max(-D_CLAMP, min(D_CLAMP, d1 - sigma * sqrt_t))
    return d1, d2


def bsm_price(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> float:
    """Price a European option with BSM and continuous dividend yield q."""
    option_kind = _validate_kind(kind)
    if T <= 0.0:
        return intrinsic(S, K, option_kind)
    if sigma <= 0.0:
        return _discounted_forward_intrinsic(S, K, T, r, q, option_kind)

    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    discounted_spot = S * math.exp(-q * T)
    discounted_strike = K * math.exp(-r * T)
    if option_kind == "call":
        return discounted_spot * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
    return discounted_strike * norm_cdf(-d2) - discounted_spot * norm_cdf(-d1)


def parity_gap(S: float, K: float, T: float, r: float, q: float, call: float, put: float) -> float:
    """Return C - P - (S*exp(-qT) - K*exp(-rT))."""
    return call - put - (S * math.exp(-q * T) - K * math.exp(-r * T))


def crr_price(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    kind: str,
    steps: int = 200,
    american: bool = True,
) -> float:
    """Price an option with a Cox-Ross-Rubinstein binomial tree."""
    option_kind = _validate_kind(kind)
    if steps < 1:
        raise ValueError("steps must be >= 1")
    if T <= 0.0:
        return intrinsic(S, K, option_kind)
    if sigma <= 0.0:
        value = _discounted_forward_intrinsic(S, K, T, r, q, option_kind)
        return max(value, intrinsic(S, K, option_kind)) if american else value

    if not american:
        return 0.5 * (
            _crr_tree_price(S, K, T, r, q, sigma, option_kind, steps, american=False)
            + _crr_tree_price(S, K, T, r, q, sigma, option_kind, steps + 1, american=False)
        )
    return _crr_tree_price(S, K, T, r, q, sigma, option_kind, steps, american=True)


def _crr_tree_price(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    kind: str,
    steps: int,
    american: bool,
) -> float:
    dt = T / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    growth = math.exp((r - q) * dt)
    p = (growth - d) / (u - d)
    discount = math.exp(-r * dt)

    values = [intrinsic(S * (u**j) * (d ** (steps - j)), K, kind) for j in range(steps + 1)]
    for step in range(steps - 1, -1, -1):
        next_values: list[float] = []
        for j in range(step + 1):
            continuation = discount * (p * values[j + 1] + (1.0 - p) * values[j])
            if american:
                spot = S * (u**j) * (d ** (step - j))
                continuation = max(continuation, intrinsic(spot, K, kind))
            next_values.append(continuation)
        values = next_values
    return values[0]

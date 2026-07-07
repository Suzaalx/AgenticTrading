"""Implied and realized volatility estimators for options math."""

from __future__ import annotations

import math
from collections.abc import Sequence

from sentinel.options.pricing import bsm_price

LOW_VOL = 1e-4
HIGH_VOL = 5.0
TRADING_DAYS = 252.0


def _validate_kind(kind: str) -> str:
    normalized = kind.lower()
    if normalized not in {"call", "put"}:
        raise ValueError("kind must be 'call' or 'put'")
    return normalized


def implied_vol(price: float, S: float, K: float, T: float, r: float, q: float, kind: str) -> float | None:
    """Return implied volatility, or None when price violates no-arbitrage bounds."""
    option_kind = _validate_kind(kind)
    if T <= 0.0 or price < 0.0 or S <= 0.0 or K <= 0.0:
        return None

    discounted_spot = S * math.exp(-q * T)
    discounted_strike = K * math.exp(-r * T)
    if option_kind == "call":
        lower = max(discounted_spot - discounted_strike, 0.0)
        upper = discounted_spot
    else:
        lower = max(discounted_strike - discounted_spot, 0.0)
        upper = discounted_strike
    tolerance = 1e-12
    if price < lower - tolerance or price > upper + tolerance:
        return None

    low = LOW_VOL
    high = HIGH_VOL
    low_value = bsm_price(S, K, T, r, q, low, option_kind) - price
    high_value = bsm_price(S, K, T, r, q, high, option_kind) - price
    if abs(low_value) <= 1e-12:
        return low
    if abs(high_value) <= 1e-12:
        return high
    if low_value * high_value > 0.0:
        return None

    for _ in range(200):
        mid = 0.5 * (low + high)
        mid_value = bsm_price(S, K, T, r, q, mid, option_kind) - price
        if abs(mid_value) <= 1e-12 or high - low <= 1e-8:
            return mid
        if low_value * mid_value <= 0.0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value
    return 0.5 * (low + high)


def _log_returns(values: Sequence[float]) -> list[float]:
    return [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]


def _sample_variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def realized_vol_close_to_close(closes: Sequence[float]) -> float:
    """Return annualized close-to-close realized volatility."""
    returns = _log_returns(closes)
    return math.sqrt(_sample_variance(returns) * TRADING_DAYS) if returns else 0.0


def parkinson(highs: Sequence[float], lows: Sequence[float]) -> float:
    """Return annualized Parkinson realized volatility from high/low sequences."""
    if len(highs) != len(lows):
        raise ValueError("highs and lows must have the same length")
    if not highs:
        return 0.0
    variance = sum(math.log(high / low) ** 2 for high, low in zip(highs, lows, strict=True))
    variance /= 4.0 * math.log(2.0) * len(highs)
    return math.sqrt(variance * TRADING_DAYS)


def yang_zhang(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> float:
    """Return annualized Yang-Zhang realized volatility; the default RV estimator."""
    lengths = {len(opens), len(highs), len(lows), len(closes)}
    if len(lengths) != 1:
        raise ValueError("opens, highs, lows, and closes must have the same length")
    n = len(closes)
    if n < 2:
        return 0.0

    overnight = [math.log(opens[i] / closes[i - 1]) for i in range(1, n)]
    open_close = [math.log(closes[i] / opens[i]) for i in range(1, n)]
    rs_terms = [
        math.log(highs[i] / closes[i]) * math.log(highs[i] / opens[i])
        + math.log(lows[i] / closes[i]) * math.log(lows[i] / opens[i])
        for i in range(1, n)
    ]
    overnight_var = _sample_variance(overnight)
    open_close_var = _sample_variance(open_close)
    rs_var = sum(rs_terms) / len(rs_terms)
    k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))
    variance = overnight_var + k * open_close_var + (1.0 - k) * rs_var
    return math.sqrt(max(variance, 0.0) * TRADING_DAYS)


def iv_rank(current: float, history: Sequence[float]) -> float:
    """Return IV rank: (current - min(history)) / (max(history) - min(history))."""
    if not history:
        return 0.0
    min_iv = min(history)
    max_iv = max(history)
    if max_iv == min_iv:
        return 0.0
    return (current - min_iv) / (max_iv - min_iv)


def iv_percentile(current: float, history: Sequence[float]) -> float:
    """Return the fraction of history below current IV."""
    if not history:
        return 0.0
    return sum(1 for value in history if value < current) / len(history)

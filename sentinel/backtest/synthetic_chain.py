"""Synthetic option-chain generation for deterministic backtests.

Backtest chains are model artifacts, not historical exchange quotes.  Volatility is
Yang-Zhang realized volatility over the last 21 completed bars multiplied by the
``iv_premium_factor`` setting (default 1.1, a simple premium over realized volatility).
Risk-free rates default to a constant because point-in-time rate history is optional for
this seam.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import pandas as pd

from sentinel.config.settings import load_settings
from sentinel.core.models import OptionChainSnapshot, OptionContract, OptionQuote
from sentinel.options.greeks import delta, gamma, theta, vega
from sentinel.options.iv import yang_zhang
from sentinel.options.pricing import bsm_price

SYNTHETIC_PRICING_LABEL = "synthetic pricing — not indicative of live fills"


@dataclass(frozen=True)
class OptionRuleSignal:
    """Rule-strategy request for the backtest engine to fill on the next bar."""

    action: Literal["OPEN_COVERED_CALL", "OPEN_CASH_SECURED_PUT", "HOLD"] = "HOLD"
    strategy: str | None = None
    target_delta: float = 0.30
    target_dte: int = 30
    contracts: int = 1


class SyntheticChainProvider:
    """Build point-in-time synthetic option chains from OHLCV history."""

    def __init__(
        self,
        ohlcv: pd.DataFrame,
        *,
        symbol: str,
        iv_premium_factor: float | None = None,
        synthetic_half_spread_pct: float | None = None,
        risk_free_rate: float | None = None,
        dividend_yield: float = 0.0,
        min_dte: int = 21,
        max_dte: int = 75,
    ) -> None:
        settings = load_settings()
        self.symbol = symbol.upper()
        self.frame = _normalize_frame(ohlcv)
        self.iv_premium_factor = (
            float(iv_premium_factor)
            if iv_premium_factor is not None
            else float(settings.backtest.options.iv_premium_factor)
        )
        self.synthetic_half_spread_pct = (
            float(synthetic_half_spread_pct)
            if synthetic_half_spread_pct is not None
            else float(settings.backtest.options.synthetic_half_spread_pct)
        )
        self.risk_free_rate = (
            float(risk_free_rate)
            if risk_free_rate is not None
            else float(settings.options.default_risk_free_rate)
        )
        self.dividend_yield = dividend_yield
        self.min_dte = min_dte
        self.max_dte = max_dte

    def chain_for(
        self,
        as_of: pd.Timestamp | datetime | date | str,
        *,
        price: Literal["close", "open"] = "close",
        run_id: str | None = None,
    ) -> OptionChainSnapshot:
        """Return a synthetic chain using only bars known at ``as_of``.

        ``price="open"`` uses the current bar's open as spot but only prior completed
        bars for realized volatility, supporting next-bar execution without peeking at
        that day's high/low/close.
        """

        ts = cast(pd.Timestamp, pd.Timestamp(cast(Any, as_of))).normalize()
        if ts not in self.frame.index:
            eligible = self.frame.loc[self.frame.index <= ts]
            if eligible.empty:
                msg = f"no OHLCV bars available at {ts.date().isoformat()}"
                raise ValueError(msg)
            ts = cast(pd.Timestamp, eligible.index[-1])
        current_row = cast(pd.Series, self.frame.loc[ts])
        vol_history = self.frame.loc[self.frame.index < ts] if price == "open" else self.frame.loc[self.frame.index <= ts]
        if vol_history.empty:
            vol_history = self.frame.loc[self.frame.index <= ts]
        vol_frame = vol_history.tail(21)
        rv = _yang_zhang_or_floor(vol_frame)
        sigma = max(rv * self.iv_premium_factor, 0.05)
        spot = float(cast(Any, current_row[price]))
        expiries = _monthly_expiries(ts.date(), self.min_dte, self.max_dte)
        strikes = _strike_grid(spot)
        quotes: list[OptionQuote] = []
        as_of_dt = ts.to_pydatetime().replace(tzinfo=UTC)

        for expiry in expiries:
            dte = max((expiry - ts.date()).days, 0)
            t_exp = max(dte / 365.0, 0.0)
            for strike in strikes:
                for kind in ("call", "put"):
                    mid = bsm_price(
                        spot,
                        strike,
                        t_exp,
                        self.risk_free_rate,
                        self.dividend_yield,
                        sigma,
                        kind,
                    )
                    half_spread = max(mid * (self.synthetic_half_spread_pct / 100.0), 0.05)
                    bid = max(mid - half_spread, 0.0)
                    ask = mid + half_spread
                    contract = OptionContract(
                        contract_symbol=_occ_symbol(self.symbol, expiry, kind, strike),
                        underlying=self.symbol,
                        kind=cast(Any, kind),
                        strike=Decimal(str(round(strike, 2))),
                        expiry=expiry,
                        multiplier=100,
                    )
                    quotes.append(
                        OptionQuote(
                            contract=contract,
                            bid=Decimal(str(round(bid, 4))),
                            ask=Decimal(str(round(ask, 4))),
                            last=Decimal(str(round(mid, 4))),
                            volume=0,
                            open_interest=0,
                            implied_vol=sigma,
                            ts=as_of_dt,
                            source="synthetic_bsm",
                            model_iv=sigma,
                            delta=delta(spot, strike, t_exp, self.risk_free_rate, self.dividend_yield, sigma, kind),
                            gamma=gamma(spot, strike, t_exp, self.risk_free_rate, self.dividend_yield, sigma),
                            vega=vega(spot, strike, t_exp, self.risk_free_rate, self.dividend_yield, sigma),
                            theta=theta(spot, strike, t_exp, self.risk_free_rate, self.dividend_yield, sigma, kind),
                        )
                    )

        return OptionChainSnapshot(
            run_id=run_id or f"synthetic:{self.symbol}:{ts.date().isoformat()}:{price}",
            underlying=self.symbol,
            as_of=as_of_dt,
            spot=Decimal(str(round(spot, 4))),
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            expiries=expiries,
            quotes=quotes,
            atm_iv=sigma,
            iv_rank=None,
            iv_percentile=None,
            rv_yang_zhang=rv,
            pricing_source="synthetic_bsm",
            providers_used={
                "options": "synthetic_bsm",
                "volatility": "yang_zhang_21d_x_iv_premium_factor",
                "rates": "constant",
                "label": SYNTHETIC_PRICING_LABEL,
            },
        )


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(column).lower() for column in out.columns]
    out.index = [cast(pd.Timestamp, pd.Timestamp(cast(Any, value))).normalize() for value in out.index]
    return cast(pd.DataFrame, out.sort_index())


def _yang_zhang_or_floor(frame: pd.DataFrame) -> float:
    if len(frame) < 2:
        return 0.20
    rv = yang_zhang(
        [float(value) for value in frame["open"]],
        [float(value) for value in frame["high"]],
        [float(value) for value in frame["low"]],
        [float(value) for value in frame["close"]],
    )
    return rv if math.isfinite(rv) and rv > 0.0 else 0.20


def _monthly_expiries(as_of: date, min_dte: int, max_dte: int) -> list[date]:
    expiries: list[date] = []
    cursor = date(as_of.year, as_of.month, 1)
    for _ in range(6):
        expiry = _third_friday(cursor.year, cursor.month)
        dte = (expiry - as_of).days
        if min_dte <= dte <= max_dte:
            expiries.append(expiry)
        cursor = date(cursor.year + (1 if cursor.month == 12 else 0), 1 if cursor.month == 12 else cursor.month + 1, 1)
    if expiries:
        return expiries
    fallback = as_of + timedelta(days=30)
    return [_next_friday(fallback)]


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_to_friday = (4 - first.weekday()) % 7
    return first + timedelta(days=days_to_friday + 14)


def _next_friday(value: date) -> date:
    return value + timedelta(days=(4 - value.weekday()) % 7)


def _strike_grid(spot: float) -> list[float]:
    increment = _strike_increment(spot)
    low = math.floor((spot * 0.8) / increment) * increment
    high = math.ceil((spot * 1.2) / increment) * increment
    count = round((high - low) / increment)
    return [round(low + (idx * increment), 2) for idx in range(count + 1) if low + (idx * increment) > 0]


def _strike_increment(spot: float) -> float:
    if spot < 50:
        return 1.0
    if spot < 200:
        return 2.5
    if spot < 500:
        return 5.0
    return 10.0


def _occ_symbol(symbol: str, expiry: date, kind: str, strike: float) -> str:
    strike_mills = round(strike * 1000)
    return f"{symbol}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}{strike_mills:08d}"

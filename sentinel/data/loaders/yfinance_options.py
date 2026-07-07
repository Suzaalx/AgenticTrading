"""Yahoo Finance option-chain loader."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, NamedTuple

import pandas as pd

from sentinel.core.models import OptionChainSnapshot, OptionContract, OptionQuote
from sentinel.options import greeks
from sentinel.options.iv import implied_vol


class LoadedOptionChain(NamedTuple):
    """Near-the-money option quotes loaded from a provider."""

    expiries: list[date]
    quotes: list[OptionQuote]


class YFinanceOptionsLoader:
    """Load and enrich yfinance option chains."""

    name = "yfinance_options"

    def load_option_chain(
        self,
        underlying: str,
        as_of: datetime,
        spot: float,
        risk_free_rate: float,
        dividend_yield: float,
        *,
        min_dte: int,
        max_dte: int,
    ) -> LoadedOptionChain | None:
        """Return a near-the-money option chain, or None on provider failure."""

        try:
            import yfinance as yf

            ticker = yf.Ticker(underlying)
            raw_expiries = list(getattr(ticker, "options", []) or [])
            if not raw_expiries or spot <= 0.0:
                return None
            quotes: list[OptionQuote] = []
            expiries: list[date] = []
            as_of_utc = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
            for expiry in raw_expiries:
                expiry_date = _parse_date(expiry)
                if expiry_date is None:
                    continue
                dte = (expiry_date - as_of_utc.date()).days
                if dte < min_dte or dte > max_dte:
                    continue
                chain = ticker.option_chain(expiry)
                expiry_quotes = [
                    *_quotes_from_frame(
                        getattr(chain, "calls", pd.DataFrame()),
                        "call",
                        underlying,
                        expiry_date,
                        as_of_utc,
                        spot,
                        risk_free_rate,
                        dividend_yield,
                    ),
                    *_quotes_from_frame(
                        getattr(chain, "puts", pd.DataFrame()),
                        "put",
                        underlying,
                        expiry_date,
                        as_of_utc,
                        spot,
                        risk_free_rate,
                        dividend_yield,
                    ),
                ]
                if expiry_quotes:
                    expiries.append(expiry_date)
                    quotes.extend(expiry_quotes)
            if not quotes:
                return None
            return LoadedOptionChain(expiries=sorted(set(expiries)), quotes=quotes)
        except Exception:
            return None

    def get_option_chain(
        self,
        underlying: str,
        as_of: datetime,
        spot: float,
        risk_free_rate: float,
        dividend_yield: float,
        *,
        min_dte: int,
        max_dte: int,
        run_id: str = "",
    ) -> OptionChainSnapshot | None:
        """Convenience wrapper returning an OptionChainSnapshot."""

        loaded = self.load_option_chain(
            underlying,
            as_of,
            spot,
            risk_free_rate,
            dividend_yield,
            min_dte=min_dte,
            max_dte=max_dte,
        )
        if loaded is None:
            return None
        return OptionChainSnapshot(
            run_id=run_id,
            underlying=underlying.upper(),
            as_of=as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC),
            spot=Decimal(str(spot)),
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            expiries=loaded.expiries,
            quotes=loaded.quotes,
            atm_iv=None,
            iv_rank=None,
            iv_percentile=None,
            rv_yang_zhang=None,
            pricing_source="live_chain",
            providers_used={"option_chain": self.name},
        )


def _quotes_from_frame(
    frame: pd.DataFrame,
    kind: Literal["call", "put"],
    underlying: str,
    expiry: date,
    as_of: datetime,
    spot: float,
    risk_free_rate: float,
    dividend_yield: float,
) -> Iterable[OptionQuote]:
    if frame is None or frame.empty:
        return []
    result: list[OptionQuote] = []
    lower = spot * 0.8
    upper = spot * 1.2
    for _, row in frame.iterrows():
        quote = _quote_from_row(
            row,
            kind,
            underlying,
            expiry,
            as_of,
            spot,
            risk_free_rate,
            dividend_yield,
            lower,
            upper,
        )
        if quote is not None:
            result.append(quote)
    return result


def _quote_from_row(
    row: pd.Series,
    kind: Literal["call", "put"],
    underlying: str,
    expiry: date,
    as_of: datetime,
    spot: float,
    risk_free_rate: float,
    dividend_yield: float,
    lower_strike: float,
    upper_strike: float,
) -> OptionQuote | None:
    strike = _float_or_none(row.get("strike"))
    bid = _float_or_none(row.get("bid"))
    ask = _float_or_none(row.get("ask"))
    last = _float_or_none(row.get("lastPrice"))
    if strike is None or bid is None or ask is None:
        return None
    if strike < lower_strike or strike > upper_strike:
        return None
    if bid < 0.0 or ask < 0.0 or ask == 0.0 or bid > ask:
        return None
    if last is not None and last < 0.0:
        return None

    provider_iv = _sane_iv(_float_or_none(row.get("impliedVolatility")))
    mid = (bid + ask) / 2.0
    t = max((expiry - as_of.date()).days, 0) / 365.0
    model_iv = implied_vol(mid, spot, strike, t, risk_free_rate, dividend_yield, kind)
    sigma = provider_iv if provider_iv is not None else model_iv
    quote_delta = quote_gamma = quote_vega = quote_theta = None
    if sigma is not None and sigma > 0.0 and math.isfinite(sigma):
        quote_delta = greeks.delta(spot, strike, t, risk_free_rate, dividend_yield, sigma, kind)
        quote_gamma = greeks.gamma(spot, strike, t, risk_free_rate, dividend_yield, sigma)
        quote_vega = greeks.vega(spot, strike, t, risk_free_rate, dividend_yield, sigma)
        quote_theta = greeks.theta(spot, strike, t, risk_free_rate, dividend_yield, sigma, kind)

    return OptionQuote(
        contract=OptionContract(
            contract_symbol=_contract_symbol(row, underlying, expiry, kind, strike),
            underlying=underlying.upper(),
            kind=kind,
            strike=Decimal(str(strike)),
            expiry=expiry,
        ),
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        last=Decimal(str(last)) if last is not None else None,
        volume=_int_or_zero(row.get("volume")),
        open_interest=_int_or_zero(row.get("openInterest")),
        implied_vol=provider_iv,
        ts=_parse_datetime(row.get("lastTradeDate"), as_of),
        source=YFinanceOptionsLoader.name,
        model_iv=model_iv,
        delta=quote_delta,
        gamma=quote_gamma,
        vega=quote_vega,
        theta=quote_theta,
    )


def _contract_symbol(
    row: pd.Series,
    underlying: str,
    expiry: date,
    kind: Literal["call", "put"],
    strike: float,
) -> str:
    raw = row.get("contractSymbol")
    if raw is not None:
        raw_text = str(raw).strip()
        if raw_text and raw_text.lower() != "nan":
            return raw_text
    yymmdd = expiry.strftime("%y%m%d")
    cp = "C" if kind == "call" else "P"
    strike_code = f"{round(strike * 1000):08d}"
    return f"{underlying.upper()}{yymmdd}{cp}{strike_code}"


def _sane_iv(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value if 0.01 <= value <= 5.0 else None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return None if math.isnan(result) else result
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _parse_datetime(value: Any, default: datetime) -> datetime:
    if value is None:
        return default
    parsed = pd.to_datetime(str(value), utc=True, errors="coerce")
    if pd.isna(parsed):
        return default
    return parsed.to_pydatetime()

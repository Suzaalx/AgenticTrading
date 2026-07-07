from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sentinel.core.models import OptionChainSnapshot, OptionContract, OptionQuote
from sentinel.options.candidates import build_candidates

AS_OF = datetime(2026, 7, 7, tzinfo=UTC)
EXPIRY = date(2026, 8, 21)


def _contract(kind: str, strike: str) -> OptionContract:
    return OptionContract(
        contract_symbol=f"XYZ260821{kind[0].upper()}{strike}",
        underlying="XYZ",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=EXPIRY,
    )


def _quote(kind: str, strike: str, bid: str, ask: str, delta: float) -> OptionQuote:
    return OptionQuote(
        contract=_contract(kind, strike),
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=None,
        volume=100,
        open_interest=500,
        implied_vol=0.25,
        ts=AS_OF,
        source="fixture",
        model_iv=0.25,
        delta=delta,
        gamma=0.02,
        vega=20.0,
        theta=-3.0,
    )


def _chain() -> OptionChainSnapshot:
    quotes = [
        _quote("call", "95", "7.95", "8.05", 0.70),
        _quote("call", "100", "4.95", "5.05", 0.55),
        _quote("call", "105", "2.95", "3.05", 0.30),
        _quote("call", "110", "1.45", "1.55", 0.20),
        _quote("put", "90", "1.45", "1.55", -0.20),
        _quote("put", "95", "2.95", "3.05", -0.30),
        _quote("put", "100", "4.95", "5.05", -0.55),
        _quote("put", "105", "7.95", "8.05", -0.70),
    ]
    return OptionChainSnapshot(
        run_id="run-1",
        underlying="XYZ",
        as_of=AS_OF,
        spot=Decimal("100"),
        risk_free_rate=0.04,
        dividend_yield=0.0,
        expiries=[EXPIRY],
        quotes=quotes,
        atm_iv=0.25,
        iv_rank=0.2,
        iv_percentile=0.3,
        rv_yang_zhang=0.2,
        pricing_source="live_chain",
        providers_used={"options": "fixture"},
    )


def test_build_candidates_low_iv_bullish_debit_structures() -> None:
    candidates = build_candidates(_chain(), "bullish", 70, {"iv_rank": 0.2}, {})

    assert [candidate.strategy for candidate in candidates][:2] == ["long_call", "bull_call_spread"]
    assert all(candidate.net_premium is not None for candidate in candidates)
    assert all(candidate.max_loss >= 0 for candidate in candidates)
    assert all(candidate.net_vega != 0 for candidate in candidates)
    assert all("all legs pass" in candidate.rationale_facts for candidate in candidates)


def test_build_candidates_high_iv_filters_illiquid_contracts() -> None:
    chain = _chain()
    chain.quotes[5].open_interest = 10
    candidates = build_candidates(chain, "bullish", 70, {"iv_rank": 0.8}, {})

    assert all(candidate.strategy != "bull_put_spread" for candidate in candidates)
    assert all(all(leg.contract.contract_symbol != "XYZ260821P95" for leg in candidate.legs) for candidate in candidates)


def test_build_candidates_neutral_event_returns_long_vol() -> None:
    candidates = build_candidates(_chain(), "neutral", 50, {"iv_rank": 0.2, "event_within_dte": True}, {})

    assert {candidate.strategy for candidate in candidates} == {"long_straddle", "long_strangle"}
    assert all(candidate.est_pop is not None for candidate in candidates)

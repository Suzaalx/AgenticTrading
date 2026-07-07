from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sentinel.core.models import OptionChainSnapshot, OptionContract, OptionQuote
from sentinel.options.candidates import build_candidates

AS_OF = datetime(2026, 7, 7, tzinfo=UTC)
WEEKLY_EXPIRY = date(2026, 8, 14)
MONTHLY_EXPIRY = date(2026, 8, 21)


class CandidateSettings:
    min_dte = 21
    max_dte = 60
    min_open_interest = 100
    max_rel_spread_pct = 10.0


def _contract(kind: str, strike: str, expiry: date = MONTHLY_EXPIRY) -> OptionContract:
    return OptionContract(
        contract_symbol=f"XYZ{expiry:%y%m%d}{kind[0].upper()}{strike}",
        underlying="XYZ",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=expiry,
    )


def _quote(
    kind: str,
    strike: str,
    bid: str,
    ask: str,
    delta: float | None,
    *,
    expiry: date = MONTHLY_EXPIRY,
    open_interest: int = 500,
) -> OptionQuote:
    return OptionQuote(
        contract=_contract(kind, strike, expiry),
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=None,
        volume=100,
        open_interest=open_interest,
        implied_vol=0.25,
        ts=AS_OF,
        source="fixture",
        model_iv=0.25,
        delta=delta,
        gamma=0.02,
        vega=20.0,
        theta=-3.0,
    )


def _chain(
    quotes: list[OptionQuote] | None = None,
    *,
    expiries: list[date] | None = None,
    atm_iv: float | None = 0.25,
    iv_rank: float | None = 0.2,
) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        run_id="run-1",
        underlying="XYZ",
        as_of=AS_OF,
        spot=Decimal("100"),
        risk_free_rate=0.04,
        dividend_yield=0.0,
        expiries=[WEEKLY_EXPIRY, MONTHLY_EXPIRY] if expiries is None else expiries,
        quotes=_liquid_quotes() if quotes is None else quotes,
        atm_iv=atm_iv,
        iv_rank=iv_rank,
        iv_percentile=0.3,
        rv_yang_zhang=0.2,
        pricing_source="live_chain",
        providers_used={"options": "fixture"},
    )


def _liquid_quotes(*, deltas: bool = True) -> list[OptionQuote]:
    return [
        _quote("call", "95", "7.95", "8.05", 0.70 if deltas else None),
        _quote("call", "100", "4.95", "5.05", 0.55 if deltas else None),
        _quote("call", "105", "2.95", "3.05", 0.30 if deltas else None),
        _quote("call", "110", "1.45", "1.55", 0.20 if deltas else None),
        _quote("put", "90", "1.45", "1.55", -0.20 if deltas else None),
        _quote("put", "95", "2.95", "3.05", -0.30 if deltas else None),
        _quote("put", "100", "4.95", "5.05", -0.55 if deltas else None),
        _quote("put", "105", "7.95", "8.05", -0.70 if deltas else None),
    ]


def test_high_iv_bullish_prefers_credit_spread_and_cash_secured_put() -> None:
    candidates = build_candidates(_chain(iv_rank=0.8), "bullish", 80, {"iv_rank": 0.8}, {})

    assert [candidate.strategy for candidate in candidates[:2]] == [
        "bull_put_spread",
        "cash_secured_put",
    ]
    assert candidates[0].net_premium < 0
    assert all(candidate.est_pop is not None for candidate in candidates)


def test_high_iv_bearish_prefers_credit_call_spread() -> None:
    candidates = build_candidates(_chain(iv_rank=0.9), "bearish", 65, {"iv_rank": 0.9}, {})

    assert [candidate.strategy for candidate in candidates[:3]] == [
        "bear_call_spread",
        "bear_put_spread",
        "long_put",
    ]
    assert candidates[0].max_loss > 0


def test_low_iv_bearish_prefers_debit_put_structures() -> None:
    candidates = build_candidates(_chain(), "bearish", 60, {"iv_rank": 0.1}, CandidateSettings())

    assert [candidate.strategy for candidate in candidates[:2]] == ["long_put", "bear_put_spread"]
    assert all("bearish" in candidate.rationale_facts for candidate in candidates)


def test_neutral_high_iv_without_event_reorders_long_vol_candidates() -> None:
    candidates = build_candidates(_chain(), "neutral", 50, {"iv_rank": 0.9, "has_event": False}, {})

    assert [candidate.strategy for candidate in candidates] == ["long_strangle", "long_straddle"]
    assert all(candidate.est_pop is not None for candidate in candidates)


def test_neutral_event_uses_chain_iv_rank_when_context_value_is_none() -> None:
    candidates = build_candidates(_chain(iv_rank=0.7), "neutral", 50, {"iv_rank": None, "has_event": True}, {})

    assert [candidate.strategy for candidate in candidates] == ["long_straddle", "long_strangle"]
    assert "iv_rank=0.70" in candidates[0].rationale_facts


def test_quotes_without_delta_fall_back_to_middle_strikes() -> None:
    candidates = build_candidates(_chain(_liquid_quotes(deltas=False)), "bullish", 70, {"iv_rank": 0.2}, {})

    assert [candidate.strategy for candidate in candidates] == ["long_call", "bull_put_spread"]
    assert candidates[0].legs[0].contract.strike == Decimal("105")


def test_liquidity_filters_exclude_wrong_expiry_low_oi_no_bid_bad_mid_and_wide_spread() -> None:
    quotes = [
        _quote("call", "100", "1.00", "1.05", 0.55, expiry=WEEKLY_EXPIRY),
        _quote("put", "100", "1.00", "1.05", -0.55, open_interest=99),
        _quote("call", "105", "0", "1.00", 0.30),
        _quote("put", "95", "-1.00", "0.50", -0.30),
        _quote("call", "110", "1.00", "2.00", 0.20),
    ]

    candidates = build_candidates(_chain(quotes), "bullish", 70, {"iv_rank": 0.2}, {})

    assert candidates == []


def test_empty_and_degenerate_chains_return_no_candidates() -> None:
    assert build_candidates(_chain(expiries=[date(2026, 7, 17)]), "bullish", 70, {}, {}) == []
    assert build_candidates(_chain(quotes=[]), "bullish", 70, {}, {}) == []
    assert build_candidates(_chain(), "sideways", 70, {}, {}) == []


def test_missing_call_or_put_side_skips_multi_leg_candidates() -> None:
    puts_only = [quote for quote in _liquid_quotes() if quote.contract.contract_symbol == "XYZ260821P95"]
    calls_only = [quote for quote in _liquid_quotes() if quote.contract.kind == "call"]

    bullish = build_candidates(_chain(puts_only, iv_rank=0.8), "bullish", 70, {"iv_rank": 0.8}, {})
    neutral = build_candidates(_chain(calls_only), "neutral", 50, {"iv_rank": 0.2, "has_event": True}, {})

    assert [candidate.strategy for candidate in bullish] == ["cash_secured_put"]
    assert neutral == []

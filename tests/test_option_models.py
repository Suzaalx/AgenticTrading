from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sentinel.core.models import (
    OptionChainSnapshot,
    OptionContract,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    OptionStrategyCandidate,
    OptionStrategyProposal,
    Order,
)


def _contract() -> OptionContract:
    return OptionContract(
        contract_symbol="AAPL260918C00230000",
        underlying="AAPL",
        kind="call",
        strike=Decimal("230"),
        expiry=date(2026, 9, 18),
    )


def _leg() -> OptionLeg:
    return OptionLeg(contract=_contract(), side="buy", contracts=1, limit_price=Decimal("5.10"))


def test_option_models_round_trip() -> None:
    now = datetime.now(UTC)
    quote = OptionQuote(
        contract=_contract(),
        bid=Decimal("5.00"),
        ask=Decimal("5.20"),
        last=Decimal("5.15"),
        volume=1000,
        open_interest=5000,
        implied_vol=0.25,
        ts=now,
        source="fixture",
        model_iv=0.24,
        delta=0.55,
        gamma=0.03,
        vega=0.12,
        theta=-0.02,
    )
    chain = OptionChainSnapshot(
        run_id="01RUN",
        underlying="AAPL",
        as_of=now,
        spot=Decimal("225"),
        risk_free_rate=0.04,
        dividend_yield=0.005,
        expiries=[date(2026, 9, 18)],
        quotes=[quote],
        atm_iv=0.24,
        iv_rank=0.5,
        iv_percentile=0.6,
        rv_yang_zhang=0.2,
        pricing_source="live_chain",
        providers_used={"options": "fixture"},
    )
    candidate = OptionStrategyCandidate(
        strategy="long_call",
        legs=[_leg()],
        net_premium=Decimal("510"),
        max_loss=Decimal("510"),
        max_gain=None,
        breakevens=[Decimal("235.10")],
        est_pop=0.45,
        net_delta=55.0,
        net_vega=12.0,
        net_theta=-2.0,
        liquidity_score=0.9,
        rationale_facts="liquid ATM call",
    )
    proposal = OptionStrategyProposal(
        run_id="01RUN",
        agent="trader",
        model="fake",
        created_at=now,
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0"),
        content="open",
        action="OPEN",
        strategy="long_call",
        legs=[_leg()],
        candidate_id="cand-1",
        max_loss_usd=Decimal("510"),
        time_horizon_days=30,
        entry_rationale="r",
        exit_plan="x",
        stop_loss_pct_premium=50.0,
        take_profit_pct_premium=100.0,
    )
    position = OptionPosition(
        position_id="pos-1",
        underlying="AAPL",
        strategy="long_call",
        legs=[_leg()],
        open_premium=Decimal("510"),
        max_loss=Decimal("510"),
        collateral=Decimal("0"),
        opened_at=now,
        expiry=date(2026, 9, 18),
        horizon_days=30,
        stop_loss_pct_premium=50.0,
        take_profit_pct_premium=100.0,
        source_run_id="01RUN",
    )

    for model in (quote, chain, candidate, proposal, position):
        assert type(model).model_validate(model.model_dump()) == model


def test_option_order_defaults_and_round_trip() -> None:
    order = Order(
        order_id="01O",
        run_id="01RUN",
        symbol="AAPL",
        side="buy",
        qty=Decimal("1"),
        reason="agent_decision",
        created_at=datetime.now(UTC),
        asset_type="option",
        legs=[_leg()],
        strategy="long_call",
        type="net_debit",
    )

    assert order.client_order_id
    assert order.venue == "paper"
    assert Order.model_validate(order.model_dump()) == order

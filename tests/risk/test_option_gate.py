from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.models import (
    Mandate,
    OptionChainSnapshot,
    OptionContract,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    OptionStrategyProposal,
    Portfolio,
    Position,
    ViolationCode,
)
from sentinel.risk.audit import read_audit
from sentinel.risk.gate import _has_naked_short, check_option_order

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
EXPIRY = date(2026, 8, 21)


def mandate(**option_overrides: object) -> Mandate:
    options: dict[str, object] = {
        "enabled": True,
        "underlying_universe": ["AAPL"],
        "max_loss_per_position_usd": 500.0,
        "max_total_options_max_loss_pct_equity": 15.0,
        "max_contracts_per_order": 10,
        "min_open_interest": 100,
        "max_rel_spread_pct": 20.0,
        "min_dte": 21,
        "max_dte": 60,
        "max_net_portfolio_delta_abs": 200.0,
        "max_net_portfolio_vega_abs": 2000.0,
        "max_option_orders_per_day": 4,
    }
    options.update(option_overrides)
    return Mandate(
        symbol_universe=["AAPL"],
        max_position_pct_equity=10.0,
        max_order_notional_usd=2000.0,
        max_gross_exposure_pct=80.0,
        max_daily_loss_pct=3.0,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
        options=options,
    )


def contract(
    kind: str = "call",
    strike: str = "100",
    expiry: date = EXPIRY,
    underlying: str = "AAPL",
) -> OptionContract:
    return OptionContract(
        contract_symbol=f"{underlying}{expiry:%y%m%d}{kind[0].upper()}{strike}",
        underlying=underlying,
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=expiry,
    )


def leg(
    *,
    kind: str = "call",
    side: str = "buy",
    strike: str = "100",
    contracts: int = 1,
    price: str = "1.00",
    expiry: date = EXPIRY,
    underlying: str = "AAPL",
) -> OptionLeg:
    return OptionLeg(
        contract=contract(kind, strike, expiry, underlying),
        side=side,  # type: ignore[arg-type]
        contracts=contracts,
        limit_price=Decimal(price),
    )


def quote(option_leg: OptionLeg, *, oi: int = 1000, bid: str = "0.95", ask: str = "1.05") -> OptionQuote:
    return OptionQuote(
        contract=option_leg.contract,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal("1.00"),
        volume=10,
        open_interest=oi,
        implied_vol=0.20,
        ts=NOW,
        source="test",
        model_iv=0.20,
        delta=0.50 if option_leg.contract.kind == "call" else -0.50,
        gamma=0.01,
        vega=10.0,
        theta=-1.0,
    )


def chain(legs: Sequence[OptionLeg], *, quotes: Sequence[OptionQuote] | None = None) -> OptionChainSnapshot:
    rows = list(quotes) if quotes is not None else [quote(option_leg) for option_leg in legs]
    return OptionChainSnapshot(
        run_id="run-1",
        underlying=legs[0].contract.underlying,
        as_of=NOW,
        spot=Decimal("100"),
        risk_free_rate=0.01,
        dividend_yield=0.0,
        expiries=[legs[0].contract.expiry],
        quotes=rows,
        atm_iv=0.20,
        iv_rank=None,
        iv_percentile=None,
        rv_yang_zhang=None,
        pricing_source="synthetic_bsm",
        providers_used={"test": "test"},
    )


def proposal(
    legs: Sequence[OptionLeg],
    *,
    strategy: str = "long_call",
    claimed_max_loss: str = "100",
) -> OptionStrategyProposal:
    return OptionStrategyProposal(
        run_id="run-1",
        agent="trader",
        model="fake",
        created_at=NOW,
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0"),
        content="open option",
        action="OPEN",
        strategy=strategy,  # type: ignore[arg-type]
        legs=list(legs),
        candidate_id="cand-1",
        max_loss_usd=Decimal(claimed_max_loss),
        time_horizon_days=30,
        entry_rationale="r",
        exit_plan="x",
        stop_loss_pct_premium=None,
        take_profit_pct_premium=None,
    )


def portfolio(cash: str = "10000", *positions: Position) -> Portfolio:
    return Portfolio(cash=Decimal(cash), positions=list(positions))


def stock_position(qty: str = "100") -> Position:
    return Position(
        symbol="AAPL",
        qty=Decimal(qty),
        avg_cost=Decimal("100"),
        stop_loss_pct=None,
        take_profit_pct=None,
        opened_at=NOW - timedelta(days=1),
        horizon_days=None,
        source_run_id="run-1",
    )


def option_position(max_loss: str = "1450") -> OptionPosition:
    return OptionPosition(
        position_id="pos-1",
        underlying="AAPL",
        strategy="long_call",
        legs=[leg()],
        open_premium=Decimal("100"),
        max_loss=Decimal(max_loss),
        collateral=Decimal("0"),
        opened_at=NOW - timedelta(days=1),
        expiry=EXPIRY,
        horizon_days=30,
        stop_loss_pct_premium=None,
        take_profit_pct_premium=None,
        source_run_id="run-1",
    )


def short_call_position(
    *,
    strike: str = "150",
    expiry: date = EXPIRY,
    position_id: str = "short-call-pos",
) -> OptionPosition:
    short_leg = leg(side="sell", strike=strike, expiry=expiry)
    return OptionPosition(
        position_id=position_id,
        underlying="AAPL",
        strategy="covered_call",
        legs=[short_leg],
        open_premium=Decimal("-100"),
        max_loss=Decimal("0"),
        collateral=Decimal("0"),
        opened_at=NOW - timedelta(days=1),
        expiry=expiry,
        horizon_days=30,
        stop_loss_pct_premium=None,
        take_profit_pct_premium=None,
        source_run_id="run-1",
    )


CaseBuilder = Callable[[], tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]]


def clean_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg()]
    return proposal(legs), portfolio(), mandate(), [], chain(legs), {}


def disabled_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, _, positions, ch, kwargs = clean_case()
    return prop, port, mandate(enabled=False), positions, ch, kwargs


def underlying_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(underlying="MSFT")]
    return proposal(legs), portfolio(), mandate(), [], chain(legs), {}


def daily_loss_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, man, positions, ch, kwargs = clean_case()
    kwargs["day_pnl_pct"] = -3.0
    return prop, port, man, positions, ch, kwargs


def cooldown_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, man, positions, ch, kwargs = clean_case()
    kwargs["last_order_ts"] = {"AAPL": NOW - timedelta(minutes=1)}
    return prop, port, man, positions, ch, kwargs


def order_limit_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, man, positions, ch, kwargs = clean_case()
    kwargs["orders_today"] = 4
    return prop, port, man, positions, ch, kwargs


def kill_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, man, positions, ch, kwargs = clean_case()
    kwargs["kill_engaged"] = True
    return prop, port, man, positions, ch, kwargs


def contracts_cap_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(contracts=11)]
    return proposal(legs), portfolio(), mandate(max_loss_per_position_usd=2000.0), [], chain(legs), {}


def max_loss_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(price="10.00")]
    return proposal(legs, claimed_max_loss="1"), portfolio(), mandate(), [], chain(legs), {}


def budget_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, man, _, ch, kwargs = clean_case()
    return prop, port, man, [option_position()], ch, kwargs


def naked_call_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(side="sell")]
    return proposal(legs, strategy="covered_call"), portfolio(), mandate(), [], chain(legs), {}


def collateral_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(kind="put", side="sell")]
    return proposal(legs, strategy="cash_secured_put"), portfolio("100"), mandate(max_loss_per_position_usd=20000.0), [], chain(legs), {}


def illiquid_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg()]
    return proposal(legs), portfolio(), mandate(), [], chain(legs, quotes=[quote(legs[0], oi=1)]), {}


def dte_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(expiry=date(2026, 7, 20))]
    return proposal(legs), portfolio(), mandate(), [], chain(legs), {}


def expiry_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    legs = [leg(expiry=date(2026, 7, 8))]
    return proposal(legs), portfolio(), mandate(min_dte=0), [], chain(legs), {}


def greeks_case() -> tuple[OptionStrategyProposal, Portfolio, Mandate, list[OptionPosition], OptionChainSnapshot, dict[str, object]]:
    prop, port, _, positions, ch, kwargs = clean_case()
    return prop, port, mandate(max_net_portfolio_delta_abs=1.0), positions, ch, kwargs


@pytest.mark.parametrize(
    ("code", "builder"),
    [
        ("OPTIONS_DISABLED", disabled_case),
        ("UNDERLYING_NOT_ALLOWED", underlying_case),
        ("DAILY_LOSS_HALT", daily_loss_case),
        ("COOLDOWN", cooldown_case),
        ("MAX_ORDERS_PER_DAY", order_limit_case),
        ("KILL_SWITCH", kill_case),
        ("ORDER_TOO_LARGE", contracts_cap_case),
        ("MAX_LOSS_EXCEEDED", max_loss_case),
        ("OPTIONS_BUDGET_EXCEEDED", budget_case),
        ("NAKED_SHORT_OPTION", naked_call_case),
        ("INSUFFICIENT_COLLATERAL", collateral_case),
        ("ILLIQUID_CONTRACT", illiquid_case),
        ("DTE_OUT_OF_RANGE", dte_case),
        ("EXPIRY_TOO_CLOSE", expiry_case),
        ("GREEKS_CAP_EXCEEDED", greeks_case),
    ],
)
def test_option_gate_violation_has_passing_and_failing_case(
    code: ViolationCode,
    builder: CaseBuilder,
) -> None:
    clean_prop, clean_port, clean_mandate, clean_positions, clean_chain, clean_kwargs = clean_case()
    clean_result = check_option_order(
        clean_prop,
        clean_port,
        clean_mandate,
        clean_positions,
        clean_chain,
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
        **clean_kwargs,
    )
    assert clean_result.passed
    assert code not in {violation.code for violation in clean_result.violations}

    prop, port, man, positions, ch, kwargs = builder()
    result = check_option_order(
        prop,
        port,
        man,
        positions,
        ch,
        orders_today=int(kwargs.get("orders_today", 0)),
        last_order_ts=kwargs.get("last_order_ts"),  # type: ignore[arg-type]
        kill_engaged=bool(kwargs.get("kill_engaged", False)),
        day_pnl_pct=kwargs.get("day_pnl_pct"),  # type: ignore[arg-type]
        marks={prop.legs[0].contract.underlying: Decimal("100")},
    )

    assert not result.passed
    assert code in {violation.code for violation in result.violations}


def test_naked_short_call_mislabeled_covered_call_is_vetoed() -> None:
    prop, port, man, positions, ch, kwargs = naked_call_case()
    result = check_option_order(
        prop,
        port,
        man,
        positions,
        ch,
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
        **kwargs,
    )

    assert "NAKED_SHORT_OPTION" in {violation.code for violation in result.violations}


def test_existing_covered_call_consumes_shares_before_new_short_call() -> None:
    new_expiry = date(2026, 9, 4)
    new_leg = leg(side="sell", strike="160", expiry=new_expiry)

    result = check_option_order(
        proposal([new_leg], strategy="covered_call", claimed_max_loss="0"),
        portfolio("10000", stock_position("100")),
        mandate(
            max_loss_per_position_usd=50000.0,
            max_total_options_max_loss_pct_equity=100.0,
        ),
        [short_call_position(strike="150", expiry=EXPIRY)],
        chain([new_leg]),
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
    )

    assert not result.passed
    assert "NAKED_SHORT_OPTION" in {violation.code for violation in result.violations}


def test_two_short_calls_with_two_hundred_shares_are_covered() -> None:
    new_expiry = date(2026, 9, 4)
    new_leg = leg(side="sell", strike="160", expiry=new_expiry)

    result = check_option_order(
        proposal([new_leg], strategy="covered_call", claimed_max_loss="0"),
        portfolio("10000", stock_position("200")),
        mandate(
            max_loss_per_position_usd=50000.0,
            max_total_options_max_loss_pct_equity=100.0,
        ),
        [short_call_position(strike="150", expiry=EXPIRY)],
        chain([new_leg]),
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
    )

    assert result.passed
    assert "NAKED_SHORT_OPTION" not in {violation.code for violation in result.violations}


def test_single_covered_call_still_passes() -> None:
    short_leg = leg(side="sell", strike="150")

    result = check_option_order(
        proposal([short_leg], strategy="covered_call", claimed_max_loss="0"),
        portfolio("10000", stock_position("100")),
        mandate(
            max_loss_per_position_usd=50000.0,
            max_total_options_max_loss_pct_equity=100.0,
        ),
        [],
        chain([short_leg]),
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
    )

    assert result.passed
    assert "NAKED_SHORT_OPTION" not in {violation.code for violation in result.violations}


def test_has_naked_short_consumes_share_coverage_across_call_lots() -> None:
    new_expiry = date(2026, 9, 4)
    new_leg = leg(side="sell", strike="160", expiry=new_expiry)

    assert _has_naked_short(
        [new_leg],
        portfolio("10000", stock_position("100")),
        [short_call_position(strike="150", expiry=EXPIRY)],
    )


def test_has_naked_short_allows_two_call_lots_with_two_share_lots() -> None:
    new_expiry = date(2026, 9, 4)
    new_leg = leg(side="sell", strike="160", expiry=new_expiry)

    assert not _has_naked_short(
        [new_leg],
        portfolio("10000", stock_position("200")),
        [short_call_position(strike="150", expiry=EXPIRY)],
    )


def test_has_naked_short_allows_single_covered_call() -> None:
    short_leg = leg(side="sell", strike="150")

    assert not _has_naked_short([short_leg], portfolio("10000", stock_position("100")), [])


def test_max_loss_uses_recomputed_legs_not_proposal_copy() -> None:
    prop, port, man, positions, ch, kwargs = max_loss_case()
    assert prop.max_loss_usd == Decimal("1")

    result = check_option_order(
        prop,
        port,
        man,
        positions,
        ch,
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
        **kwargs,
    )

    assert "MAX_LOSS_EXCEEDED" in {violation.code for violation in result.violations}


def test_option_gate_audit_entry_includes_legs() -> None:
    prop, port, man, positions, ch, kwargs = clean_case()

    check_option_order(
        prop,
        port,
        man,
        positions,
        ch,
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        day_pnl_pct=None,
        marks={"AAPL": Decimal("100")},
        **kwargs,
    )

    latest = read_audit(limit=1)[0]
    assert latest["kind"] == "option_gate_evaluated"
    assert latest["payload"]["legs"][0]["contract"]["contract_symbol"] == "AAPL260821C100"  # type: ignore[index]

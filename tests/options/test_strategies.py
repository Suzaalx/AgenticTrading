from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sentinel.core.models import OptionContract
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
    payoff_at_expiry,
)

EXPIRY = date(2026, 9, 18)


def _contract(kind: str, strike: str) -> OptionContract:
    return OptionContract(
        contract_symbol=f"XYZ260918{kind[0].upper()}{strike}",
        underlying="XYZ",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=EXPIRY,
    )


def _grid_assert(legs: list, expected_gain: Decimal | None = None) -> None:
    payoffs = [payoff_at_expiry(legs, Decimal(i)) for i in range(0, 301)]
    assert min(payoffs) == pytest.approx(float(-max_loss(legs)), abs=1e-6)
    gain = max_gain(legs)
    if expected_gain is not None:
        assert gain == expected_gain
    if gain is not None:
        assert max(payoffs) == pytest.approx(float(gain), abs=1e-6)


def test_single_leg_premium_loss_and_breakeven() -> None:
    call = build_long_call(_contract("call", "100"), limit_price=Decimal("5"))
    put = build_long_put(_contract("put", "100"), limit_price=Decimal("4"))

    assert net_premium(call) == Decimal("500")
    assert max_loss(call) == Decimal("500")
    assert max_gain(call) is None
    assert breakevens(call) == [Decimal("105")]
    assert max_loss(put) == Decimal("400")
    assert breakevens(put) == [Decimal("96")]


def test_spread_closed_form_economics() -> None:
    bull_call = build_bull_call_spread(
        _contract("call", "100"), _contract("call", "110"), long_price=Decimal("6"), short_price=Decimal("2")
    )
    bear_put = build_bear_put_spread(
        _contract("put", "110"), _contract("put", "100"), long_price=Decimal("7"), short_price=Decimal("3")
    )
    bull_put = build_bull_put_spread(
        _contract("put", "100"), _contract("put", "90"), short_price=Decimal("4"), long_price=Decimal("1")
    )
    bear_call = build_bear_call_spread(
        _contract("call", "100"), _contract("call", "110"), short_price=Decimal("4"), long_price=Decimal("1")
    )

    assert max_loss(bull_call) == Decimal("400")
    assert max_gain(bull_call) == Decimal("600")
    assert breakevens(bull_call) == [Decimal("104")]
    assert max_loss(bear_put) == Decimal("400")
    assert max_gain(bear_put) == Decimal("600")
    assert breakevens(bear_put) == [Decimal("106")]
    assert max_loss(bull_put) == Decimal("700")
    assert max_gain(bull_put) == Decimal("300")
    assert breakevens(bull_put) == [Decimal("97")]
    assert max_loss(bear_call) == Decimal("700")
    assert max_gain(bear_call) == Decimal("300")
    assert breakevens(bear_call) == [Decimal("103")]


@pytest.mark.parametrize(
    "legs",
    [
        build_bull_call_spread(_contract("call", "100"), _contract("call", "110"), long_price=Decimal("6"), short_price=Decimal("2")),
        build_bear_put_spread(_contract("put", "110"), _contract("put", "100"), long_price=Decimal("7"), short_price=Decimal("3")),
        build_bull_put_spread(_contract("put", "100"), _contract("put", "90"), short_price=Decimal("4"), long_price=Decimal("1")),
        build_bear_call_spread(_contract("call", "100"), _contract("call", "110"), short_price=Decimal("4"), long_price=Decimal("1")),
        build_cash_secured_put(_contract("put", "100"), limit_price=Decimal("3")),
        build_long_straddle(_contract("call", "100"), _contract("put", "100"), call_price=Decimal("5"), put_price=Decimal("4")),
        build_long_strangle(_contract("put", "95"), _contract("call", "105"), put_price=Decimal("3"), call_price=Decimal("4")),
    ],
)
def test_payoff_grid_matches_closed_form_for_defined_risk(legs: list) -> None:
    _grid_assert(legs)


def test_straddle_and_strangle_breakevens() -> None:
    straddle = build_long_straddle(
        _contract("call", "100"), _contract("put", "100"), call_price=Decimal("5"), put_price=Decimal("4")
    )
    strangle = build_long_strangle(
        _contract("put", "95"), _contract("call", "105"), put_price=Decimal("3"), call_price=Decimal("4")
    )

    assert breakevens(straddle) == [Decimal("91"), Decimal("109")]
    assert breakevens(strangle) == [Decimal("88"), Decimal("112")]

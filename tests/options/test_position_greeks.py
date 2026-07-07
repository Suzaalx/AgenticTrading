from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sentinel.core.models import OptionContract
from sentinel.options.greeks import position_greeks, sum_position_greeks
from sentinel.options.strategies import build_long_call, build_long_straddle

EXPIRY = date(2027, 1, 15)


def _contract(kind: str, strike: str) -> OptionContract:
    return OptionContract(
        contract_symbol=f"XYZ270115{kind[0].upper()}{strike}",
        underlying="XYZ",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=EXPIRY,
    )


def test_position_greeks_long_call_scales_by_contract_multiplier() -> None:
    legs = build_long_call(_contract("call", "100"), contracts=2, limit_price=Decimal("5"))
    greeks = position_greeks(legs, 100.0, 0.05, 0.0, 0.20, T=1.0)

    assert greeks.delta == pytest.approx(2 * 100 * 0.6368, abs=1e-2)


def test_position_greeks_atm_straddle_is_nearly_delta_neutral() -> None:
    legs = build_long_straddle(
        _contract("call", "100"), _contract("put", "100"), call_price=Decimal("5"), put_price=Decimal("5")
    )
    greeks = position_greeks(legs, 100.0, 0.0, 0.0, 0.20, T=0.01)

    assert greeks.delta == pytest.approx(0.0, abs=1.0)
    assert greeks.vega > 0.0
    assert greeks.theta < 0.0


def test_sum_position_greeks() -> None:
    first = position_greeks(build_long_call(_contract("call", "100"), limit_price=Decimal("5")), 100.0, 0.05, 0.0, 0.20, T=1.0)
    second = position_greeks(build_long_call(_contract("call", "110"), limit_price=Decimal("2")), 100.0, 0.05, 0.0, 0.20, T=1.0)

    total = sum_position_greeks([first, second])
    assert total.delta == pytest.approx(first.delta + second.delta)
    assert total.gamma == pytest.approx(first.gamma + second.gamma)

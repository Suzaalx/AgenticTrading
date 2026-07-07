from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sentinel.core.models import Mandate, OptionContract, OptionLeg, OptionPosition, Portfolio
from sentinel.risk.sizing import available_cash, size_option_order


def mandate() -> Mandate:
    return Mandate(
        symbol_universe=["AAPL"],
        max_position_pct_equity=10.0,
        max_order_notional_usd=2000.0,
        max_gross_exposure_pct=80.0,
        max_daily_loss_pct=3.0,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
        options={"enabled": True, "max_loss_per_position_usd": 500.0},
    )


def option_position(collateral: str) -> OptionPosition:
    expiry = date(2026, 8, 21)
    contract = OptionContract(
        contract_symbol="AAPL260821P100",
        underlying="AAPL",
        kind="put",
        strike=Decimal("100"),
        expiry=expiry,
    )
    leg = OptionLeg(contract=contract, side="sell", contracts=1, limit_price=Decimal("1"))
    return OptionPosition(
        position_id="pos-1",
        underlying="AAPL",
        strategy="cash_secured_put",
        legs=[leg],
        open_premium=Decimal("-100"),
        max_loss=Decimal("9900"),
        collateral=Decimal(collateral),
        opened_at=datetime(2026, 7, 7, tzinfo=UTC),
        expiry=expiry,
        horizon_days=30,
        stop_loss_pct_premium=None,
        take_profit_pct_premium=None,
        source_run_id="run-1",
    )


def test_option_sizing_uses_max_loss_budget_floor() -> None:
    assert (
        size_option_order(
            equity=Decimal("10000"),
            mandate=mandate(),
            conviction=50,
            trader_quantity_pct=100,
            approved_quantity_pct=100,
            max_loss_per_1_spread=Decimal("120"),
        )
        == 4
    )


def test_option_sizing_pm_scale_can_only_shrink() -> None:
    assert (
        size_option_order(
            equity=Decimal("10000"),
            mandate=mandate(),
            conviction=50,
            trader_quantity_pct=100,
            approved_quantity_pct=50,
            max_loss_per_1_spread=Decimal("120"),
        )
        == 4
    )
    assert (
        size_option_order(
            equity=Decimal("10000"),
            mandate=mandate(),
            conviction=10,
            trader_quantity_pct=100,
            approved_quantity_pct=25,
            max_loss_per_1_spread=Decimal("120"),
        )
        == 2
    )


def test_option_sizing_returns_zero_for_non_positive_max_loss() -> None:
    assert (
        size_option_order(
            equity=Decimal("10000"),
            mandate=mandate(),
            conviction=100,
            trader_quantity_pct=100,
            approved_quantity_pct=100,
            max_loss_per_1_spread=Decimal("0"),
        )
        == 0
    )


def test_available_cash_reduces_cash_by_escrowed_collateral() -> None:
    assert available_cash(Portfolio(cash=Decimal("10000")), [option_position("2500")]) == Decimal(
        "7500"
    )

from __future__ import annotations

from decimal import Decimal

from sentinel.core.models import Mandate
from sentinel.risk.sizing import pm_scale, size_order, target_notional


def mandate() -> Mandate:
    return Mandate(
        symbol_universe=["AAPL", "BTC-USD"],
        max_position_pct_equity=10.0,
        max_order_notional_usd=2000.0,
        max_gross_exposure_pct=80.0,
        max_daily_loss_pct=3.0,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
    )


def test_target_notional_math_with_pm_scale() -> None:
    assert target_notional(
        equity=Decimal("10000"),
        mandate=mandate(),
        quantity_pct=Decimal("50"),
        trader_quantity_pct=Decimal("50"),
        approved_quantity_pct=Decimal("25"),
    ) == Decimal("250.000")


def test_whole_share_floor_for_equities() -> None:
    assert size_order(
        symbol="AAPL",
        equity=Decimal("10000"),
        price=Decimal("20"),
        mandate=mandate(),
        quantity_pct=Decimal("33"),
        trader_quantity_pct=Decimal("33"),
        approved_quantity_pct=Decimal("33"),
    ) == Decimal("16")


def test_crypto_quantizes_to_six_decimal_places() -> None:
    assert size_order(
        symbol="BTC-USD",
        equity=Decimal("10000"),
        price=Decimal("30000"),
        mandate=mandate(),
        quantity_pct=Decimal("50"),
        trader_quantity_pct=Decimal("50"),
        approved_quantity_pct=Decimal("25"),
    ) == Decimal("0.008333")


def test_pm_scale_clamps_to_one() -> None:
    assert pm_scale(Decimal("50"), Decimal("80")) == Decimal("1")
    assert size_order(
        symbol="AAPL",
        equity=Decimal("10000"),
        price=Decimal("20"),
        mandate=mandate(),
        quantity_pct=Decimal("50"),
        trader_quantity_pct=Decimal("50"),
        approved_quantity_pct=Decimal("80"),
    ) == Decimal("25")


def test_zero_and_edge_quantity_pct_returns_zero() -> None:
    assert size_order(
        symbol="AAPL",
        equity=Decimal("10000"),
        price=Decimal("20"),
        mandate=mandate(),
        quantity_pct=Decimal("0"),
        trader_quantity_pct=Decimal("50"),
        approved_quantity_pct=Decimal("25"),
    ) == Decimal("0")
    assert size_order(
        symbol="AAPL",
        equity=Decimal("10000"),
        price=Decimal("0"),
        mandate=mandate(),
        quantity_pct=Decimal("50"),
        trader_quantity_pct=Decimal("50"),
        approved_quantity_pct=Decimal("25"),
    ) == Decimal("0")

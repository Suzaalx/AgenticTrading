from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.models import OptionContract, OptionLeg, OptionPosition, Order
from sentinel.options.strategies import (
    build_bull_call_spread,
    build_cash_secured_put,
    build_covered_call,
    build_long_call,
)
from sentinel.risk.audit import read_audit
from sentinel.risk.killswitch import disengage, engage
from sentinel.risk.monitor import PositionMonitor

NOW = datetime(2026, 1, 10, 20, 0, tzinfo=UTC)
EXPIRY = date(2026, 1, 10)
FUTURE_EXPIRY = date(2026, 1, 20)


def contract(symbol: str, kind: str, strike: str, expiry: date = FUTURE_EXPIRY) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="AAPL",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=expiry,
    )


def position(
    legs: Sequence[OptionLeg],
    *,
    open_premium: str = "200",
    opened_at: datetime = NOW - timedelta(days=1),
    expiry: date = FUTURE_EXPIRY,
    stop: float | None = 25.0,
    take: float | None = 25.0,
    horizon: int | None = 10,
) -> OptionPosition:
    return OptionPosition(
        position_id="opt-1",
        underlying="AAPL",
        strategy="long_call",
        legs=list(legs),
        open_premium=Decimal(open_premium),
        max_loss=Decimal("200"),
        collateral=Decimal("0"),
        opened_at=opened_at,
        expiry=expiry,
        horizon_days=horizon,
        stop_loss_pct_premium=stop,
        take_profit_pct_premium=take,
        source_run_id="run-1",
    )


async def run_monitor(pos: OptionPosition, marks: dict[str, Decimal], handler: object | None = None) -> list[Order]:
    captured: list[Order] = []
    on_order = handler if handler is not None else captured.append
    monitor = PositionMonitor(
        positions_provider=lambda: [],
        marks_provider=lambda: {},
        on_order=lambda order: None,
        option_positions_provider=lambda: [pos],
        option_marks_provider=lambda _: marks,
        on_option_order=on_order,  # type: ignore[arg-type]
        force_close_dte=1,
    )
    orders = await monitor.run_once(NOW)
    if handler is None:
        assert captured == orders
    return orders


@pytest.mark.parametrize(
    ("marks", "reason"),
    [
        ({"AAPL260120C00100000": Decimal("1")}, "stop_loss"),
        ({"AAPL260120C00100000": Decimal("3")}, "take_profit"),
    ],
)
async def test_option_monitor_premium_stop_and_take_profit(marks: dict[str, Decimal], reason: str) -> None:
    legs = build_long_call(contract("AAPL260120C00100000", "call", "100"))

    orders = await run_monitor(position(legs), marks)

    assert len(orders) == 1
    assert orders[0].reason == reason
    assert orders[0].asset_type == "option"


async def test_option_monitor_horizon_time_exit() -> None:
    legs = build_long_call(contract("AAPL260120C00100000", "call", "100"))
    pos = position(legs, opened_at=NOW - timedelta(days=3), horizon=2, stop=None, take=None)

    orders = await run_monitor(pos, {"AAPL260120C00100000": Decimal("2")})

    assert orders[0].reason == "time_exit"


async def test_option_monitor_force_closes_at_dte_one() -> None:
    legs = build_long_call(contract("AAPL260111C00100000", "call", "100", date(2026, 1, 11)))
    pos = position(legs, expiry=date(2026, 1, 11), stop=None, take=None, horizon=None)

    orders = await run_monitor(pos, {"AAPL260111C00100000": Decimal("2")})

    assert orders[0].reason == "time_exit"


@pytest.mark.parametrize(
    ("legs", "underlying", "expected_value", "expected_side"),
    [
        (build_long_call(contract("AAPL260110C00100000", "call", "100", EXPIRY)), "120", "2000", "sell"),
        (build_covered_call(contract("AAPL260110C00100000", "call", "100", EXPIRY)), "120", "2000", "buy"),
        (build_cash_secured_put(contract("AAPL260110P00100000", "put", "100", EXPIRY)), "90", "1000", "buy"),
        (
            build_bull_call_spread(
                contract("AAPL260110C00100000", "call", "100", EXPIRY),
                contract("AAPL260110C00110000", "call", "110", EXPIRY),
            ),
            "120",
            "1000",
            "sell",
        ),
        (build_long_call(contract("AAPL260110C00100000", "call", "100", EXPIRY)), "90", "0", "sell"),
    ],
)
async def test_option_monitor_expiry_settlement_branches(
    legs: list[OptionLeg], underlying: str, expected_value: str, expected_side: str
) -> None:
    pos = position(legs, expiry=EXPIRY, stop=None, take=None, horizon=None)
    marks = {leg.contract.contract_symbol: Decimal("1") for leg in legs}
    marks["AAPL"] = Decimal(underlying)

    orders = await run_monitor(pos, marks, handler=lambda _: False)

    assert [order.reason for order in orders] == ["time_exit", "expiry_settlement"]
    assert orders[1].qty == Decimal(expected_value)
    assert orders[1].side == expected_side
    assert read_audit()[-1]["kind"] == "option_expiry_settlement"


async def test_kill_switch_halts_option_execution() -> None:
    engage("tester")
    try:
        legs = build_long_call(contract("AAPL260120C00100000", "call", "100"))
        orders = await run_monitor(position(legs), {"AAPL260120C00100000": Decimal("1")})
    finally:
        disengage("tester")

    assert orders == []

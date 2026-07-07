from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.models import Mandate, Order, Portfolio, Position, ViolationCode
from sentinel.risk.gate import check_order

NOW = datetime(2026, 1, 1, 12, 0, 0)


def mandate(**overrides: object) -> Mandate:
    values = {
        "symbol_universe": ["AAPL", "SPY"],
        "max_position_pct_equity": 10.0,
        "max_order_notional_usd": 2000.0,
        "max_gross_exposure_pct": 80.0,
        "max_daily_loss_pct": 3.0,
        "max_orders_per_day": 10,
        "allow_short": False,
        "cooldown_minutes_per_symbol": 60,
    }
    values.update(overrides)
    return Mandate.model_validate(values)


def order(symbol: str = "AAPL", side: str = "buy", qty: str = "1") -> Order:
    return Order(
        order_id="order-1",
        run_id="run-1",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        qty=Decimal(qty),
        type="market",
        reason="agent_decision",
        created_at=NOW,
    )


def portfolio(*positions: Position, cash: str = "10000", day_pnl: str = "0") -> Portfolio:
    return Portfolio(cash=Decimal(cash), positions=list(positions), day_pnl=Decimal(day_pnl))


def position(symbol: str, qty: str, avg_cost: str = "100") -> Position:
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        avg_cost=Decimal(avg_cost),
        stop_loss_pct=None,
        take_profit_pct=None,
        opened_at=NOW - timedelta(days=1),
        horizon_days=None,
        source_run_id="run-1",
    )


@pytest.mark.parametrize(
    ("name", "test_order", "test_portfolio", "kwargs", "expected"),
    [
        (
            "symbol universe",
            order(symbol="MSFT"),
            portfolio(),
            {},
            {"SYMBOL_NOT_IN_UNIVERSE"},
        ),
        (
            "order too large",
            order(qty="30"),
            portfolio(),
            {},
            {"ORDER_TOO_LARGE"},
        ),
        (
            "exposure cap",
            order(qty="2"),
            portfolio(position("SPY", "79"), cash="2100"),
            {"marks": {"AAPL": Decimal("100"), "SPY": Decimal("100")}},
            {"EXPOSURE_CAP"},
        ),
        (
            "daily loss halt",
            order(qty="1"),
            portfolio(),
            {"day_pnl_pct": -3.0},
            {"DAILY_LOSS_HALT"},
        ),
        (
            "cooldown",
            order(qty="1"),
            portfolio(),
            {"last_order_ts": {"AAPL": NOW - timedelta(minutes=59, seconds=59)}},
            {"COOLDOWN"},
        ),
        (
            "short not allowed",
            order(side="sell", qty="1"),
            portfolio(),
            {},
            {"SHORT_NOT_ALLOWED"},
        ),
        (
            "kill switch",
            order(qty="1"),
            portfolio(),
            {"kill_engaged": True},
            {"KILL_SWITCH"},
        ),
        (
            "max orders per day",
            order(qty="1"),
            portfolio(),
            {"orders_today": 10},
            {"MAX_ORDERS_PER_DAY"},
        ),
    ],
)
def test_gate_violation_cases(
    name: str,
    test_order: Order,
    test_portfolio: Portfolio,
    kwargs: dict[str, object],
    expected: set[ViolationCode],
) -> None:
    result = check_order(
        test_order,
        test_portfolio,
        mandate(),
        orders_today=int(kwargs.get("orders_today", 0)),  # type: ignore[arg-type]
        last_order_ts=kwargs.get("last_order_ts"),  # type: ignore[arg-type]
        kill_engaged=bool(kwargs.get("kill_engaged", False)),
        marks=kwargs.get("marks", {"AAPL": Decimal("100")}),  # type: ignore[arg-type]
        day_pnl_pct=kwargs.get("day_pnl_pct"),  # type: ignore[arg-type]
    )

    codes = {violation.code for violation in result.violations}
    assert not result.passed, name
    assert expected <= codes


def test_gate_clean_pass() -> None:
    result = check_order(
        order(qty="1"),
        portfolio(),
        mandate(),
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        marks={"AAPL": Decimal("100")},
        day_pnl_pct=None,
    )

    assert result.passed
    assert result.violations == []


def test_gate_collects_all_expected_violations() -> None:
    result = check_order(
        order(symbol="BAD", side="sell", qty="30"),
        portfolio(),
        mandate(),
        orders_today=0,
        last_order_ts={"BAD": NOW - timedelta(minutes=1)},
        kill_engaged=True,
        marks={"BAD": Decimal("100")},
        day_pnl_pct=-5.0,
    )

    assert {violation.code for violation in result.violations} == {
        "SYMBOL_NOT_IN_UNIVERSE",
        "ORDER_TOO_LARGE",
        "DAILY_LOSS_HALT",
        "COOLDOWN",
        "SHORT_NOT_ALLOWED",
        "KILL_SWITCH",
    }


def test_daily_loss_halt_exempts_risk_reducing_closing_orders() -> None:
    result = check_order(
        order(side="sell", qty="5"),
        portfolio(position("AAPL", "10")),
        mandate(),
        orders_today=0,
        last_order_ts=None,
        kill_engaged=False,
        marks={"AAPL": Decimal("100")},
        day_pnl_pct=-10.0,
    )

    assert result.passed
    assert "DAILY_LOSS_HALT" not in {violation.code for violation in result.violations}


def test_cooldown_boundary_allows_order_at_exact_limit() -> None:
    result = check_order(
        order(qty="1"),
        portfolio(),
        mandate(),
        orders_today=0,
        last_order_ts={"AAPL": NOW - timedelta(minutes=60)},
        kill_engaged=False,
        marks={"AAPL": Decimal("100")},
        day_pnl_pct=None,
    )

    assert result.passed


def test_max_orders_per_day_exempts_risk_reducing_closing_orders() -> None:
    result = check_order(
        order(side="sell", qty="5"),
        portfolio(position("AAPL", "10")),
        mandate(),
        orders_today=50,
        last_order_ts=None,
        kill_engaged=False,
        marks={"AAPL": Decimal("100")},
        day_pnl_pct=None,
    )

    assert result.passed
    assert "MAX_ORDERS_PER_DAY" not in {violation.code for violation in result.violations}

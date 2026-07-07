from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.core.models import Position
from sentinel.risk.monitor import PositionMonitor, evaluate_position

NOW = datetime(2026, 1, 10, 12, 0, 0)


def position(**overrides: object) -> Position:
    values = {
        "symbol": "AAPL",
        "qty": Decimal("10"),
        "avg_cost": Decimal("100"),
        "stop_loss_pct": 10.0,
        "take_profit_pct": 10.0,
        "opened_at": NOW - timedelta(days=1),
        "horizon_days": 10,
        "source_run_id": "run-1",
    }
    values.update(overrides)
    return Position.model_validate(values)


def test_stop_loss_produces_mechanical_exit_order() -> None:
    exit_order = evaluate_position(position(), Decimal("90"), NOW)

    assert exit_order is not None
    assert exit_order.reason == "stop_loss"
    assert exit_order.side == "sell"
    assert exit_order.qty == Decimal("10")


def test_take_profit_produces_mechanical_exit_order() -> None:
    exit_order = evaluate_position(position(), Decimal("110"), NOW)

    assert exit_order is not None
    assert exit_order.reason == "take_profit"
    assert exit_order.side == "sell"


def test_time_exit_produces_mechanical_exit_order() -> None:
    exit_order = evaluate_position(
        position(stop_loss_pct=None, take_profit_pct=None, opened_at=NOW - timedelta(days=3), horizon_days=2),
        Decimal("100"),
        NOW,
    )

    assert exit_order is not None
    assert exit_order.reason == "time_exit"


def test_no_trigger_returns_none() -> None:
    assert evaluate_position(position(), Decimal("100"), NOW) is None


async def test_position_monitor_run_once_emits_triggered_orders() -> None:
    captured = []
    monitor = PositionMonitor(
        positions_provider=lambda: [position()],
        marks_provider=lambda: {"AAPL": Decimal("90")},
        on_order=captured.append,
    )

    orders = await monitor.run_once(NOW)

    assert len(orders) == 1
    assert captured == orders

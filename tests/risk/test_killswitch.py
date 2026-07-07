from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sentinel.core.models import Mandate, Order, Portfolio
from sentinel.risk.audit import read_audit
from sentinel.risk.gate import check_order
from sentinel.risk.killswitch import disengage, engage, is_engaged


def test_killswitch_engage_disengage_and_gate_violation() -> None:
    mandate = Mandate(
        symbol_universe=["AAPL"],
        max_position_pct_equity=10.0,
        max_order_notional_usd=2000.0,
        max_gross_exposure_pct=80.0,
        max_daily_loss_pct=3.0,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
    )
    test_order = Order(
        order_id="order-1",
        run_id="run-1",
        symbol="AAPL",
        side="buy",
        qty=Decimal("1"),
        type="market",
        reason="agent_decision",
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )

    engage("tester")
    assert is_engaged()
    result = check_order(
        test_order,
        Portfolio(cash=Decimal("10000")),
        mandate,
        orders_today=0,
        last_order_ts=None,
        kill_engaged=is_engaged(),
        marks={"AAPL": Decimal("100")},
        day_pnl_pct=None,
    )
    assert "KILL_SWITCH" in {violation.code for violation in result.violations}

    disengage("tester")
    assert not is_engaged()
    rows = read_audit()
    kill_rows = [row for row in rows if row["kind"] == "kill_switch_changed"]
    assert [row["payload"] for row in kill_rows] == [{"enabled": True}, {"enabled": False}]

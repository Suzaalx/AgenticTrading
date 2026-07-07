from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.models import Mandate, OptionContract, OptionLeg, Order, Quote
from sentinel.risk.live_mandate import LiveMandate, to_marketable_limit

NOW = datetime(2026, 7, 7, 16, 0, tzinfo=UTC)


def _mandate(**live_overrides: object) -> Mandate:
    return Mandate.model_validate(
        {
            "symbol_universe": ["AAPL", "BTC-USD", "XYZ"],
            "max_position_pct_equity": 10.0,
            "max_order_notional_usd": 2000.0,
            "max_gross_exposure_pct": 80.0,
            "max_daily_loss_pct": 3.0,
            "max_orders_per_day": 10,
            "allow_short": False,
            "cooldown_minutes_per_symbol": 60,
            "live": {
                "crypto_stage_enabled": True,
                "equity_stage_enabled": True,
                "options_stage_enabled": True,
                "max_live_order_notional_usd": 200.0,
                "max_live_daily_loss_usd": 100.0,
                "max_live_orders_per_day": 3,
                "max_account_allocation_usd": 500.0,
                "require_limit_orders": True,
                "quote_max_age_seconds": 60,
                **live_overrides,
            },
        }
    )


def _order(**overrides: object) -> Order:
    data = {
        "order_id": "order-1",
        "run_id": "run-1",
        "symbol": "BTC-USD",
        "side": "buy",
        "qty": Decimal("1"),
        "type": "limit",
        "reason": "agent_decision",
        "created_at": NOW,
        "asset_type": "crypto",
        "venue": "robinhood_crypto",
    }
    data.update(overrides)
    return Order.model_validate(data)


def _context(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "now": NOW,
        "price": Decimal("100"),
        "quote_ts": NOW,
        "broker_day_pnl": Decimal("0"),
        "live_orders_today": 0,
        "live_positions": [],
    }
    data.update(overrides)
    return data


def _codes(result: object) -> list[str]:
    return [violation.code for violation in result.violations]  # type: ignore[attr-defined]


def test_fail_closed_when_live_context_is_missing() -> None:
    result = LiveMandate(_mandate()).check_live_order(
        _order(),
        now=NOW,
    )

    assert not result.passed
    assert _codes(result).count("LIVE_NOTIONAL_EXCEEDED") == 1
    assert "LIVE_ORDER_LIMIT" in _codes(result)
    assert "QUOTE_TOO_OLD" in _codes(result)
    assert _codes(result).count("RECONCILIATION_MISMATCH") == 2


def test_stage_market_reconciliation_tick_and_daily_loss_codes_are_reported() -> None:
    result = LiveMandate(_mandate(crypto_stage_enabled=False)).check_live_order(
        _order(type="market"),
        **_context(
            reconciliation_stale_ticks="3",
            broker_day_pnl=Decimal("-500"),
            live_orders_today="3",
        ),
    )

    codes = _codes(result)
    assert codes.count("LIVE_STAGE_NOT_ENABLED") == 2
    assert "RECONCILIATION_MISMATCH" in codes
    assert "LIVE_ORDER_LIMIT" in codes
    assert "LIVE_DAILY_LOSS_HALT" not in codes


def test_live_mandate_uses_quote_mapping_and_realized_unrealized_pnl() -> None:
    result = LiveMandate(_mandate()).check_live_order(
        _order(),
        **_context(
            price=None,
            quote={"ask": "101", "bid": "99"},
            broker_day_pnl=None,
            broker_realized_day_pnl="-40.25",
            broker_unrealized_day_pnl="-60.00",
        ),
    )

    assert not result.passed
    assert "LIVE_DAILY_LOSS_HALT" in _codes(result)
    assert "LIVE_NOTIONAL_EXCEEDED" not in _codes(result)


def test_live_mandate_aligns_naive_quote_ts_and_accounts_for_position_shapes() -> None:
    class Position:
        qty = Decimal("2")
        mark = Decimal("50")

    result = LiveMandate(_mandate()).check_live_order(
        _order(),
        **_context(
            quote_ts=(NOW - timedelta(seconds=30)).replace(tzinfo=None),
            live_positions={
                "mapped": {"quantity": "4", "avg_cost": "100"},
                "object": Position(),
            },
        ),
    )

    assert not result.passed
    assert "QUOTE_TOO_OLD" not in _codes(result)
    assert "LIVE_NOTIONAL_EXCEEDED" in _codes(result)


def test_option_leg_notional_requires_all_leg_prices() -> None:
    contract = OptionContract(
        contract_symbol="XYZ260821C00100000",
        underlying="XYZ",
        kind="call",
        strike=Decimal("100"),
        expiry=NOW.date(),
    )
    missing_price_order = _order(
        symbol="XYZ",
        asset_type="option",
        venue="robinhood_agentic",
        qty=Decimal("1"),
        legs=[OptionLeg(contract=contract, side="buy", contracts=1, limit_price=None)],
    )

    result = LiveMandate(_mandate()).check_live_order(missing_price_order, **_context(price=None))

    assert not result.passed
    assert "LIVE_NOTIONAL_EXCEEDED" in _codes(result)


def test_option_leg_notional_uses_contract_multiplier() -> None:
    contract = OptionContract(
        contract_symbol="XYZ260821C00100000",
        underlying="XYZ",
        kind="call",
        strike=Decimal("100"),
        expiry=NOW.date(),
        multiplier=100,
    )
    option_order = _order(
        symbol="XYZ",
        asset_type="option",
        venue="robinhood_agentic",
        qty=Decimal("1"),
        legs=[OptionLeg(contract=contract, side="buy", contracts=1, limit_price=Decimal("2.01"))],
    )

    result = LiveMandate(_mandate()).check_live_order(option_order, **_context(price=None))

    assert not result.passed
    assert "LIVE_NOTIONAL_EXCEEDED" in _codes(result)


def test_to_marketable_limit_handles_sell_mapping_and_missing_price() -> None:
    sell_order = _order(side="sell", type="market")

    limit_order, limit_price = to_marketable_limit(sell_order, {"bid": "99.50"}, buffer_bps=50)

    assert limit_order.type == "limit"
    assert limit_price == Decimal("99.00")
    with pytest.raises(ValueError, match="quote must include"):
        to_marketable_limit(sell_order, {"ask": "100"})


def test_to_marketable_limit_uses_quote_price_for_buy() -> None:
    limit_order, limit_price = to_marketable_limit(
        _order(type="market"),
        Quote(symbol="BTC-USD", price=Decimal("10.00"), ts=NOW, source="fixture"),
    )

    assert limit_order.type == "limit"
    assert limit_price == Decimal("10.01")

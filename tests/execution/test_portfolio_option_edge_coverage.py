
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from sentinel.core.models import (
    Fill,
    OptionContract,
    OptionLeg,
    OptionPosition,
    Order,
    Portfolio,
    Position,
)
from sentinel.execution.portfolio import (
    PortfolioAccounting,
    apply_fill,
    apply_option_fill,
    load_option_positions,
    marked_equity,
    open_portfolio_connection,
    save_option_positions,
)
from sentinel.risk.sizing import available_cash

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
EXPIRY = date(2026, 8, 21)


def contract(symbol: str, kind: str, strike: str) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="AAPL",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=EXPIRY,
    )


def leg(symbol: str, kind: str, strike: str, side: str, limit: str | None = None) -> OptionLeg:
    return OptionLeg(
        contract=contract(symbol, kind, strike),
        side=side,  # type: ignore[arg-type]
        contracts=1,
        limit_price=None if limit is None else Decimal(limit),
    )


def option_order(order_id: str, legs: list[OptionLeg], order_type: str) -> Order:
    return Order(
        order_id=order_id,
        run_id="run-1",
        symbol="AAPL",
        side="buy" if order_type == "net_debit" else "sell",
        qty=Decimal("1"),
        type=order_type,  # type: ignore[arg-type]
        reason="agent_decision",
        created_at=NOW,
        asset_type="option",
        legs=legs,
        strategy="long_call" if len(legs) == 1 and legs[0].side == "buy" else None,
    )


def equity_order(order_id: str = "stock", side: str = "buy") -> Order:
    return Order(
        order_id=order_id,
        run_id="run-1",
        symbol="AAPL",
        side=side,  # type: ignore[arg-type]
        qty=Decimal("1"),
        type="limit",
        reason="agent_decision",
        created_at=NOW,
    )


def fill(order_id: str, price: str, qty: str = "1", commission: str = "0") -> Fill:
    return Fill(
        order_id=order_id,
        price=Decimal(price),
        qty=Decimal(qty),
        ts=NOW,
        slippage_usd=Decimal("0"),
        commission_usd=Decimal(commission),
    )


def test_apply_fill_rejects_invalid_stock_fill_edges() -> None:
    portfolio = Portfolio(cash=Decimal("50"))
    with pytest.raises(ValueError, match="must match"):
        apply_fill(portfolio, equity_order(), fill("other", "10"))
    with pytest.raises(ValueError, match="positive"):
        apply_fill(portfolio, equity_order(), fill("stock", "10", qty="0"))
    with pytest.raises(ValueError, match="cash negative"):
        apply_fill(portfolio, equity_order(), fill("stock", "100"))
    with pytest.raises(ValueError, match="without an open position"):
        apply_fill(portfolio, equity_order(side="sell"), fill("stock", "10"))

    long = Portfolio(
        cash=Decimal("0"),
        positions=[
            Position(
                symbol="AAPL",
                qty=Decimal("1"),
                avg_cost=Decimal("10"),
                stop_loss_pct=None,
                take_profit_pct=None,
                opened_at=NOW,
                horizon_days=None,
                source_run_id=None,
            )
        ],
    )
    with pytest.raises(ValueError, match="only 1 available"):
        apply_fill(long, equity_order(side="sell"), fill("stock", "10", qty="2"))


def test_option_credit_debit_close_and_collateral_release_branches() -> None:
    cash_secured_put = [leg("AAPL260821P00100000", "put", "100", "sell", "1.50")]
    portfolio = Portfolio(cash=Decimal("20000"))

    portfolio, options, open_realized = apply_option_fill(
        portfolio,
        [],
        option_order("short-put", cash_secured_put, "net_credit"),
        fill("short-put", "-150", commission="1"),
    )

    assert portfolio.cash == Decimal("20149")
    assert options[0].collateral == Decimal("10000")
    assert available_cash(portfolio, options) == Decimal("10149")
    assert open_realized == Decimal("-1")

    close_legs = [cash_secured_put[0].model_copy(update={"side": "buy"})]
    portfolio, options, close_realized = apply_option_fill(
        portfolio,
        options,
        option_order("close-put", close_legs, "net_debit"),
        fill("close-put", "25", commission="1"),
    )

    assert options == []
    assert portfolio.cash == Decimal("20123")
    assert close_realized == Decimal("124")
    assert available_cash(portfolio, options) == portfolio.cash

    debit_call = [leg("AAPL260821C00100000", "call", "100", "buy")]
    portfolio, options, debit_realized = apply_option_fill(
        Portfolio(cash=Decimal("10000")),
        [],
        option_order("long-call", debit_call, "net_debit"),
        fill("long-call", "250", commission="1"),
    )
    assert portfolio.cash == Decimal("9749")
    assert options[0].collateral == Decimal("0")
    assert debit_realized == Decimal("-1")


def test_option_fill_validation_and_available_cash_failure() -> None:
    legs = [leg("AAPL260821C00100000", "call", "100", "buy")]
    with pytest.raises(ValueError, match="must match"):
        apply_option_fill(Portfolio(cash=Decimal("1000")), [], option_order("open", legs, "net_debit"), fill("x", "1"))
    with pytest.raises(ValueError, match="option order"):
        apply_option_fill(Portfolio(cash=Decimal("1000")), [], equity_order(), fill("stock", "1"))
    with pytest.raises(ValueError, match="positive"):
        apply_option_fill(Portfolio(cash=Decimal("1000")), [], option_order("open", legs, "net_debit"), fill("open", "1", qty="0"))
    with pytest.raises(ValueError, match="available cash negative"):
        apply_option_fill(Portfolio(cash=Decimal("100")), [], option_order("open", legs, "net_debit"), fill("open", "250"))


def test_marked_equity_uses_option_marks_and_intrinsic_fallbacks() -> None:
    long_call = leg("AAPL260821C00100000", "call", "100", "buy")
    long_put = leg("AAPL260821P00110000", "put", "110", "buy")
    short_call = leg("AAPL260821C00120000", "call", "120", "sell", "1.25")
    portfolio = Portfolio(cash=Decimal("10000"))
    options = [
        OptionPosition(
            position_id="marks",
            underlying="AAPL",
            strategy="long_strangle",
            legs=[long_call, long_put, short_call],
            open_premium=Decimal("0"),
            max_loss=Decimal("0"),
            collateral=Decimal("0"),
            opened_at=NOW,
            expiry=EXPIRY,
            horizon_days=None,
            stop_loss_pct_premium=None,
            take_profit_pct_premium=None,
            source_run_id=None,
        )
    ]

    assert marked_equity(
        portfolio,
        option_positions=options,
        option_marks={"AAPL260821C00100000": Decimal("7")},
        underlying_marks={"AAPL": Decimal("105")},
    ) == portfolio.equity + Decimal("1075")
    assert marked_equity(portfolio, option_positions=options) == portfolio.equity - Decimal("125")


def test_load_save_option_positions_replaces_removed_rows(tmp_path) -> None:
    db_path = tmp_path / "portfolio.db"
    legs = [leg("AAPL260821C00100000", "call", "100", "buy")]
    _, positions, _ = apply_option_fill(
        Portfolio(cash=Decimal("10000")),
        [],
        option_order("persist", legs, "net_debit"),
        fill("persist", "100"),
    )
    with open_portfolio_connection(db_path) as conn:
        save_option_positions(conn, positions)
        assert load_option_positions(conn) == positions
        save_option_positions(conn, [])
        assert load_option_positions(conn) == []


def test_portfolio_accounting_persists_options_and_snapshots(tmp_path) -> None:
    accounting = PortfolioAccounting(tmp_path / "accounting.db", starting_cash=Decimal("10000"))
    legs = [leg("AAPL260821C00100000", "call", "100", "buy")]

    portfolio, options, realized, snapshot = accounting.apply_option_fill(
        option_order("managed", legs, "net_debit"),
        fill("managed", "100", commission="1"),
        option_marks={"AAPL260821C00100000": Decimal("2")},
    )
    event = PortfolioAccounting.equity_event(snapshot)
    follow_up = accounting.snapshot(underlying_marks={"AAPL": Decimal("103")})

    assert realized == Decimal("-1")
    assert portfolio.cash == Decimal("9899")
    assert options[0].position_id == "managed"
    assert snapshot.equity == Decimal("10099")
    assert follow_up.equity == Decimal("10199")
    assert event.equity == snapshot.equity

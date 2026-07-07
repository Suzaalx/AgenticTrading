from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from sentinel.core.models import (
    Fill,
    OptionContract,
    OptionLeg,
    OptionQuote,
    Order,
    Portfolio,
    Position,
)
from sentinel.execution.broker import OrderRejected
from sentinel.execution.paper import PaperBroker
from sentinel.execution.portfolio import (
    apply_option_fill,
    load_option_positions,
    marked_equity,
    open_portfolio_connection,
    save_option_positions,
)
from sentinel.options.strategies import (
    build_bull_call_spread,
    build_cash_secured_put,
    build_covered_call,
    build_long_call,
)
from sentinel.risk.sizing import available_cash

NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
EXPIRY = date(2026, 2, 20)


def contract(symbol: str, kind: str, strike: str) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="AAPL",
        kind=kind,  # type: ignore[arg-type]
        strike=Decimal(strike),
        expiry=EXPIRY,
    )


def option_order(order_id: str, legs: Sequence[OptionLeg], order_type: str) -> Order:
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
        legs=list(legs),
    )


def fill(order_id: str, price: str, commission: str = "1.30") -> Fill:
    return Fill(
        order_id=order_id,
        price=Decimal(price),
        qty=Decimal("1"),
        ts=NOW,
        slippage_usd=Decimal("0"),
        commission_usd=Decimal(commission),
    )


def reverse(legs: Sequence[OptionLeg]) -> list[OptionLeg]:
    return [leg.model_copy(update={"side": "sell" if leg.side == "buy" else "buy"}) for leg in legs]


@pytest.mark.parametrize(
    ("legs", "open_price", "close_price", "expected"),
    [
        (build_long_call(contract("AAPL260220C00100000", "call", "100")), "200", "-300", "97.40"),
        (
            build_bull_call_spread(
                contract("AAPL260220C00100000", "call", "100"),
                contract("AAPL260220C00110000", "call", "110"),
            ),
            "250",
            "-400",
            "147.40",
        ),
        (build_cash_secured_put(contract("AAPL260220P00100000", "put", "100")), "-150", "50", "97.40"),
        (build_covered_call(contract("AAPL260220C00110000", "call", "110")), "-100", "30", "67.40"),
    ],
)
def test_option_open_close_round_trips_cash_and_realized_pnl(
    legs: list[OptionLeg], open_price: str, close_price: str, expected: str
) -> None:
    portfolio = Portfolio(
        cash=Decimal("20000"),
        positions=[
            Position(
                symbol="AAPL",
                qty=Decimal("100"),
                avg_cost=Decimal("100"),
                stop_loss_pct=None,
                take_profit_pct=None,
                opened_at=NOW,
                horizon_days=None,
                source_run_id="stock",
            )
        ],
    )
    options = []
    open_order = option_order("open", legs, "net_debit" if Decimal(open_price) > 0 else "net_credit")
    portfolio, options, open_realized = apply_option_fill(portfolio, options, open_order, fill("open", open_price))
    assert portfolio.cash >= Decimal("0")
    assert available_cash(portfolio, options) >= Decimal("0")

    close_legs = reverse(legs)
    close_order = option_order("close", close_legs, "net_debit" if Decimal(close_price) > 0 else "net_credit")
    portfolio, options, close_realized = apply_option_fill(
        portfolio, options, close_order, fill("close", close_price)
    )

    assert portfolio.cash >= Decimal("0")
    assert options == []
    assert open_realized + close_realized == Decimal(expected)


def test_option_positions_persist_and_marked_equity_includes_option_marks(tmp_path) -> None:
    db_path = tmp_path / "portfolio.db"
    legs = build_long_call(contract("AAPL260220C00100000", "call", "100"))
    portfolio, positions, _ = apply_option_fill(
        Portfolio(cash=Decimal("10000")),
        [],
        option_order("open", legs, "net_debit"),
        fill("open", "200"),
    )
    with open_portfolio_connection(db_path) as conn:
        save_option_positions(conn, positions)
        loaded = load_option_positions(conn)

    assert loaded == positions
    assert marked_equity(
        portfolio,
        option_positions=loaded,
        option_marks={"AAPL260220C00100000": Decimal("3")},
    ) == portfolio.equity + Decimal("300")


class OptionQuoteSource:
    def __init__(self, quotes: dict[str, OptionQuote]) -> None:
        self.quotes = quotes

    def get_option_quote(self, contract: OptionContract) -> OptionQuote:
        return self.quotes[contract.contract_symbol]


def quote(contract_: OptionContract, bid: str, ask: str) -> OptionQuote:
    return OptionQuote(
        contract=contract_,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=None,
        volume=100,
        open_interest=100,
        implied_vol=None,
        ts=NOW,
        source="fake",
        model_iv=None,
        delta=None,
        gamma=None,
        vega=None,
        theta=None,
    )


async def test_paper_broker_option_fill_uses_atomic_slipped_net_price() -> None:
    call = contract("AAPL260220C00100000", "call", "100")
    put = contract("AAPL260220P00090000", "put", "90")
    legs = [OptionLeg(contract=call, side="buy", contracts=1, limit_price=None), OptionLeg(contract=put, side="sell", contracts=1, limit_price=None)]
    broker = PaperBroker(
        OptionQuoteSource(
            {
                call.contract_symbol: quote(call, "1.90", "2.10"),
                put.contract_symbol: quote(put, "0.90", "1.10"),
            }
        ),
        clock=lambda: NOW,
    )

    result = await broker.submit(option_order("spread", legs, "net_debit"))

    assert not isinstance(result, OrderRejected)
    assert result.price == Decimal("105.00")
    assert result.slippage_usd == Decimal("5.00")
    assert result.commission_usd == Decimal("1.30")

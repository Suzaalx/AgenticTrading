"""Paper portfolio accounting and persistence helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from sentinel.config.settings import load_settings
from sentinel.core.events import EquityUpdated
from sentinel.core.ids import new_id
from sentinel.core.models import Fill, OptionLeg, OptionPosition, Order, Portfolio, Position
from sentinel.options.strategies import infer_strategy, max_loss
from sentinel.risk.sizing import available_cash
from sentinel.store.db import connect, run_migrations


@dataclass(frozen=True)
class EquitySnapshot:
    """One persisted equity-curve point."""

    ts: datetime
    equity: Decimal
    cash: Decimal
    day_pnl: Decimal


def apply_fill(portfolio: Portfolio, order: Order, fill: Fill) -> tuple[Portfolio, Decimal]:
    """Apply a fill to a portfolio and return the updated state plus realized P&L.

    Buy commissions are returned as negative realized P&L so a complete round trip sums to
    quote price delta minus all slippage and commissions while keeping avg_cost as fill price.
    """

    if order.order_id != fill.order_id:
        msg = "fill.order_id must match order.order_id"
        raise ValueError(msg)
    if fill.qty <= Decimal("0"):
        msg = "fill quantity must be positive"
        raise ValueError(msg)

    positions = [position.model_copy() for position in portfolio.positions]
    position_index = _find_position_index(positions, order.symbol)

    if order.side == "buy":
        notional = fill.price * fill.qty
        new_cash = portfolio.cash - notional - fill.commission_usd
        if new_cash < Decimal("0"):
            msg = "buy fill would make cash negative"
            raise ValueError(msg)
        if position_index is None:
            positions.append(
                Position(
                    symbol=order.symbol,
                    qty=fill.qty,
                    avg_cost=fill.price,
                    stop_loss_pct=None,
                    take_profit_pct=None,
                    opened_at=fill.ts,
                    horizon_days=None,
                    source_run_id=order.run_id,
                )
            )
        else:
            existing = positions[position_index]
            combined_qty = existing.qty + fill.qty
            combined_cost = (existing.qty * existing.avg_cost) + (fill.qty * fill.price)
            positions[position_index] = existing.model_copy(
                update={"qty": combined_qty, "avg_cost": combined_cost / combined_qty}
            )
        realized_pnl = -fill.commission_usd
        return (
            Portfolio(
                cash=new_cash,
                positions=positions,
                day_pnl=portfolio.day_pnl + realized_pnl,
            ),
            realized_pnl,
        )

    if position_index is None:
        msg = f"cannot sell {order.symbol} without an open position"
        raise ValueError(msg)
    existing = positions[position_index]
    if fill.qty > existing.qty:
        msg = f"cannot sell {fill.qty} {order.symbol}; only {existing.qty} available"
        raise ValueError(msg)

    proceeds = fill.price * fill.qty
    new_cash = portfolio.cash + proceeds - fill.commission_usd
    realized_pnl = (fill.price - existing.avg_cost) * fill.qty - fill.commission_usd
    remaining_qty = existing.qty - fill.qty
    if remaining_qty == Decimal("0"):
        del positions[position_index]
    else:
        positions[position_index] = existing.model_copy(update={"qty": remaining_qty})
    return (
        Portfolio(
            cash=new_cash,
            positions=positions,
            day_pnl=portfolio.day_pnl + realized_pnl,
        ),
        realized_pnl,
    )


def unrealized_pnl(portfolio: Portfolio, marks: dict[str, Decimal]) -> Decimal:
    """Return mark-to-market P&L for open positions."""

    return sum(
        (
            position.qty * (marks.get(position.symbol, position.avg_cost) - position.avg_cost)
            for position in portfolio.positions
        ),
        Decimal("0"),
    )


def apply_option_fill(
    portfolio: Portfolio,
    option_positions: list[OptionPosition],
    order: Order,
    fill: Fill,
) -> tuple[Portfolio, list[OptionPosition], Decimal]:
    """Apply an atomic option-strategy fill and return updated state plus realized P&L."""

    if order.order_id != fill.order_id:
        msg = "fill.order_id must match order.order_id"
        raise ValueError(msg)
    if order.asset_type != "option" or not order.legs:
        msg = "option fill requires an option order with legs"
        raise ValueError(msg)
    if fill.qty <= Decimal("0"):
        msg = "fill quantity must be positive"
        raise ValueError(msg)

    positions = [position.model_copy(deep=True) for position in option_positions]
    close_index = _find_reversed_option_position(positions, order.legs)
    if close_index is not None:
        existing = positions[close_index]
        new_cash = portfolio.cash - fill.price - fill.commission_usd
        realized_pnl = (fill.price * Decimal("-1") - existing.open_premium) - fill.commission_usd
        del positions[close_index]
        updated = Portfolio(
            cash=new_cash,
            positions=[position.model_copy() for position in portfolio.positions],
            day_pnl=portfolio.day_pnl + realized_pnl,
        )
        return updated, positions, realized_pnl

    strategy = order.strategy or infer_strategy(order.legs)
    premium = fill.price
    new_cash = portfolio.cash - premium - fill.commission_usd
    collateral = _option_collateral(order.legs, premium)
    expiry = min(leg.contract.expiry for leg in order.legs)
    try:
        loss = max_loss(order.legs, stock_qty=_stock_notional(portfolio, order.legs[0].contract.underlying))
    except ValueError:
        loss = max(premium, Decimal("0")) + collateral
    new_position = OptionPosition(
        position_id=order.order_id or new_id(),
        underlying=order.legs[0].contract.underlying,
        strategy=strategy,
        legs=[leg.model_copy(deep=True) for leg in order.legs],
        open_premium=premium,
        max_loss=loss,
        collateral=collateral,
        opened_at=fill.ts,
        expiry=expiry,
        horizon_days=None,
        stop_loss_pct_premium=None,
        take_profit_pct_premium=None,
        source_run_id=order.run_id,
        venue="paper",
    )
    updated_positions = [*positions, new_position]
    updated = Portfolio(
        cash=new_cash,
        positions=[position.model_copy() for position in portfolio.positions],
        day_pnl=portfolio.day_pnl - fill.commission_usd,
    )
    if available_cash(updated, updated_positions) < Decimal("0"):
        msg = "option fill would make available cash negative"
        raise ValueError(msg)
    return updated, updated_positions, -fill.commission_usd


def marked_equity(
    portfolio: Portfolio,
    marks: dict[str, Decimal] | None = None,
    *,
    option_positions: list[OptionPosition] | None = None,
    option_marks: dict[str, Decimal] | None = None,
    underlying_marks: dict[str, Decimal] | None = None,
) -> Decimal:
    """Compute marked equity, including options when explicit option positions are supplied.

    Option marks should be chain mids keyed by OCC contract symbol. Missing long-leg marks fall
    back to intrinsic value when an underlying mark is available; missing short-leg marks fall
    back to the last leg limit price (the last known mid), or zero.
    """

    equity = portfolio.marked_equity(marks or {})
    for position in option_positions or []:
        equity += _option_position_value(position, option_marks or {}, underlying_marks or {})
    return equity


def open_portfolio_connection(path: str | Path | None = None) -> sqlite3.Connection:
    """Open SQLite and ensure tables exist."""

    conn = connect(path)
    run_migrations(conn)
    return conn


def load_positions(conn: sqlite3.Connection) -> list[Position]:
    """Load all open positions from SQLite."""

    rows = conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
    return [
        Position(
            symbol=cast(str, row["symbol"]),
            qty=Decimal(cast(str, row["qty"])),
            avg_cost=Decimal(cast(str, row["avg_cost"])),
            stop_loss_pct=cast(float | None, row["stop_pct"]),
            take_profit_pct=cast(float | None, row["tp_pct"]),
            horizon_days=cast(int | None, row["horizon_days"]),
            opened_at=datetime.fromisoformat(cast(str, row["opened_at"])),
            source_run_id=cast(str | None, row["source_run_id"]),
        )
        for row in rows
    ]


def save_positions(conn: sqlite3.Connection, portfolio: Portfolio) -> None:
    """Persist the portfolio's open positions, removing positions closed in memory."""

    symbols = [position.symbol for position in portfolio.positions]
    with conn:
        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            conn.execute(f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})", symbols)
        else:
            conn.execute("DELETE FROM positions")
        for position in portfolio.positions:
            conn.execute(
                """INSERT OR REPLACE INTO positions
                (symbol, qty, avg_cost, stop_pct, tp_pct, horizon_days, opened_at, source_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position.symbol,
                    str(position.qty),
                    str(position.avg_cost),
                    position.stop_loss_pct,
                    position.take_profit_pct,
                    position.horizon_days,
                    position.opened_at.isoformat(),
                    position.source_run_id,
                ),
            )


def load_option_positions(conn: sqlite3.Connection) -> list[OptionPosition]:
    """Load all open option strategy positions from SQLite."""

    rows = conn.execute("SELECT * FROM option_positions ORDER BY position_id").fetchall()
    positions: list[OptionPosition] = []
    for row in rows:
        legs = [
            OptionLeg.model_validate(item)
            for item in json.loads(cast(str, row["legs_json"]))
        ]
        positions.append(
            OptionPosition(
                position_id=cast(str, row["position_id"]),
                underlying=cast(str, row["underlying"]),
                strategy=cast(str, row["strategy"]),  # type: ignore[arg-type]
                legs=legs,
                open_premium=Decimal(cast(str, row["open_premium"])),
                max_loss=Decimal(cast(str, row["max_loss"])),
                collateral=Decimal(cast(str, row["collateral"])),
                opened_at=datetime.fromisoformat(cast(str, row["opened_at"])),
                expiry=datetime.fromisoformat(cast(str, row["expiry"])).date(),
                horizon_days=cast(int | None, row["horizon_days"]),
                stop_loss_pct_premium=cast(float | None, row["stop_loss_pct_premium"]),
                take_profit_pct_premium=cast(float | None, row["take_profit_pct_premium"]),
                source_run_id=cast(str | None, row["source_run_id"]),
                venue=cast(str, row["venue"] or "paper"),  # type: ignore[arg-type]
            )
        )
    return positions


def save_option_positions(conn: sqlite3.Connection, positions: list[OptionPosition]) -> None:
    """Persist open option strategy positions, removing positions closed in memory."""

    ids = [position.position_id for position in positions]
    with conn:
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(f"DELETE FROM option_positions WHERE position_id NOT IN ({placeholders})", ids)
        else:
            conn.execute("DELETE FROM option_positions")
        for position in positions:
            conn.execute(
                """INSERT OR REPLACE INTO option_positions
                (position_id, underlying, strategy, legs_json, open_premium, max_loss, collateral,
                 opened_at, expiry, horizon_days, stop_loss_pct_premium, take_profit_pct_premium,
                 source_run_id, venue)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position.position_id,
                    position.underlying,
                    position.strategy,
                    json.dumps(
                        [leg.model_dump(mode="json") for leg in position.legs],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    str(position.open_premium),
                    str(position.max_loss),
                    str(position.collateral),
                    position.opened_at.isoformat(),
                    position.expiry.isoformat(),
                    position.horizon_days,
                    position.stop_loss_pct_premium,
                    position.take_profit_pct_premium,
                    position.source_run_id,
                    position.venue,
                ),
            )


def load_portfolio(
    conn: sqlite3.Connection, *, starting_cash: Decimal | None = None
) -> Portfolio:
    """Read current portfolio from persisted positions and the latest cash snapshot."""

    latest = conn.execute(
        "SELECT cash, day_pnl FROM equity_curve ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        cash = starting_cash if starting_cash is not None else _settings_starting_cash()
        day_pnl = Decimal("0")
    else:
        cash = Decimal(str(latest["cash"]))
        day_pnl = Decimal(str(latest["day_pnl"]))
    return Portfolio(cash=cash, positions=load_positions(conn), day_pnl=day_pnl)


def append_equity_snapshot(
    conn: sqlite3.Connection,
    portfolio: Portfolio,
    *,
    marks: dict[str, Decimal] | None = None,
    option_positions: list[OptionPosition] | None = None,
    option_marks: dict[str, Decimal] | None = None,
    underlying_marks: dict[str, Decimal] | None = None,
    ts: datetime | None = None,
) -> EquitySnapshot:
    """Append and return an equity-curve snapshot."""

    snapshot_ts = ts or datetime.now(UTC)
    equity = marked_equity(
        portfolio,
        marks or {},
        option_positions=option_positions,
        option_marks=option_marks,
        underlying_marks=underlying_marks,
    )
    snapshot = EquitySnapshot(
        ts=snapshot_ts,
        equity=equity,
        cash=portfolio.cash,
        day_pnl=portfolio.day_pnl,
    )
    conn.execute(
        "INSERT INTO equity_curve (ts, equity, cash, day_pnl) VALUES (?, ?, ?, ?)",
        (
            snapshot.ts.isoformat(),
            str(snapshot.equity),
            str(snapshot.cash),
            str(snapshot.day_pnl),
        ),
    )
    conn.commit()
    return snapshot


class PortfolioAccounting:
    """Stateful portfolio accounting backed by Sentinel SQLite."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        starting_cash: Decimal | None = None,
    ) -> None:
        self._db_path = db_path
        self._starting_cash = starting_cash

    def load(self) -> Portfolio:
        """Load the current persisted portfolio."""

        with open_portfolio_connection(self._db_path) as conn:
            return load_portfolio(conn, starting_cash=self._starting_cash)

    def apply_fill(
        self,
        order: Order,
        fill: Fill,
        *,
        marks: dict[str, Decimal] | None = None,
    ) -> tuple[Portfolio, Decimal, EquitySnapshot]:
        """Apply, persist, and snapshot a fill."""

        with open_portfolio_connection(self._db_path) as conn:
            portfolio = load_portfolio(conn, starting_cash=self._starting_cash)
            updated, realized_pnl = apply_fill(portfolio, order, fill)
            save_positions(conn, updated)
            snapshot = append_equity_snapshot(conn, updated, marks=marks)
        return updated, realized_pnl, snapshot

    def apply_option_fill(
        self,
        order: Order,
        fill: Fill,
        *,
        marks: dict[str, Decimal] | None = None,
        option_marks: dict[str, Decimal] | None = None,
        underlying_marks: dict[str, Decimal] | None = None,
    ) -> tuple[Portfolio, list[OptionPosition], Decimal, EquitySnapshot]:
        """Apply, persist, and snapshot an option fill."""

        with open_portfolio_connection(self._db_path) as conn:
            portfolio = load_portfolio(conn, starting_cash=self._starting_cash)
            option_positions = load_option_positions(conn)
            updated, updated_options, realized_pnl = apply_option_fill(
                portfolio, option_positions, order, fill
            )
            save_positions(conn, updated)
            save_option_positions(conn, updated_options)
            snapshot = append_equity_snapshot(
                conn,
                updated,
                marks=marks,
                option_positions=updated_options,
                option_marks=option_marks,
                underlying_marks=underlying_marks,
            )
        return updated, updated_options, realized_pnl, snapshot

    def snapshot(
        self,
        portfolio: Portfolio | None = None,
        *,
        marks: dict[str, Decimal] | None = None,
        option_positions: list[OptionPosition] | None = None,
        option_marks: dict[str, Decimal] | None = None,
        underlying_marks: dict[str, Decimal] | None = None,
    ) -> EquitySnapshot:
        """Persist a monitor-tick equity snapshot."""

        with open_portfolio_connection(self._db_path) as conn:
            current = portfolio if portfolio is not None else load_portfolio(
                conn, starting_cash=self._starting_cash
            )
            current_options = option_positions if option_positions is not None else load_option_positions(conn)
            return append_equity_snapshot(
                conn,
                current,
                marks=marks,
                option_positions=current_options,
                option_marks=option_marks,
                underlying_marks=underlying_marks,
            )

    @staticmethod
    def equity_event(snapshot: EquitySnapshot) -> EquityUpdated:
        """Convert a snapshot to the core event consumed by monitors and the TUI."""

        return EquityUpdated(
            ts=snapshot.ts,
            equity=snapshot.equity,
            cash=snapshot.cash,
            day_pnl=snapshot.day_pnl,
        )


def _find_position_index(positions: list[Position], symbol: str) -> int | None:
    for index, position in enumerate(positions):
        if position.symbol == symbol:
            return index
    return None


def _find_reversed_option_position(
    positions: list[OptionPosition], legs: list[OptionLeg]
) -> int | None:
    target = sorted(_leg_key(_reverse_leg(leg)) for leg in legs)
    for index, position in enumerate(positions):
        if sorted(_leg_key(leg) for leg in position.legs) == target:
            return index
    return None


def _reverse_leg(leg: OptionLeg) -> OptionLeg:
    return leg.model_copy(update={"side": "sell" if leg.side == "buy" else "buy"})


def _leg_key(leg: OptionLeg) -> tuple[str, str, int]:
    return (leg.contract.contract_symbol, leg.side, leg.contracts)


def _option_collateral(legs: list[OptionLeg], premium: Decimal) -> Decimal:
    shorts = [leg for leg in legs if leg.side == "sell"]
    if premium >= 0 or not shorts:
        return Decimal("0")
    if len(legs) == 1 and shorts[0].contract.kind == "put":
        leg = shorts[0]
        return leg.contract.strike * Decimal(leg.contract.multiplier) * Decimal(leg.contracts)
    if len(legs) == 2:
        strikes = sorted({leg.contract.strike for leg in legs})
        if len(strikes) == 2:
            contracts = Decimal(max(leg.contracts for leg in legs))
            multiplier = Decimal(legs[0].contract.multiplier)
            return (strikes[1] - strikes[0]) * multiplier * contracts
    return Decimal("0")


def _stock_notional(portfolio: Portfolio, underlying: str) -> Decimal:
    return sum(
        (
            position.qty * position.avg_cost
            for position in portfolio.positions
            if position.symbol == underlying and position.qty > 0
        ),
        Decimal("0"),
    )


def _option_position_value(
    position: OptionPosition,
    option_marks: dict[str, Decimal],
    underlying_marks: dict[str, Decimal],
) -> Decimal:
    total = Decimal("0")
    for leg in position.legs:
        mark = option_marks.get(leg.contract.contract_symbol)
        if mark is None:
            mark = _fallback_option_mark(leg, underlying_marks.get(position.underlying))
        sign = Decimal("1") if leg.side == "buy" else Decimal("-1")
        total += sign * mark * Decimal(leg.contracts) * Decimal(leg.contract.multiplier)
    return total


def _fallback_option_mark(leg: OptionLeg, underlying_mark: Decimal | None) -> Decimal:
    if leg.side == "sell":
        return leg.limit_price or Decimal("0")
    if underlying_mark is None:
        return Decimal("0")
    if leg.contract.kind == "call":
        return max(underlying_mark - leg.contract.strike, Decimal("0"))
    return max(leg.contract.strike - underlying_mark, Decimal("0"))


def _settings_starting_cash() -> Decimal:
    return Decimal(str(load_settings().execution.starting_cash_usd))

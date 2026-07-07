"""Live broker reconciliation and working-order lifecycle tracking."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from sentinel.core.bus import EventBus
from sentinel.core.events import EquityUpdated, OrderFilled, ReconciliationFailed
from sentinel.core.models import Fill, OptionLeg, OptionPosition, Order, Portfolio, Position
from sentinel.execution.broker import LiveBroker, Venue
from sentinel.execution.ledger import append_fill, update_order_status
from sentinel.execution.portfolio import (
    append_equity_snapshot,
    apply_fill,
    apply_option_fill,
    load_option_positions,
    load_portfolio,
    save_option_positions,
    save_positions,
)
from sentinel.risk.audit import append_audit

LiveOrderStatus = Literal["working", "partial", "filled", "cancelled", "rejected"]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ReconciliationResult:
    """Result for one venue reconciliation pass."""

    venue: Venue
    ok: bool
    diff: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class ReconciliationRun:
    """Aggregate result for a one-shot reconciler run."""

    results: list[ReconciliationResult]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def diff(self) -> dict[str, object]:
        return {result.venue: result.diff for result in self.results if result.diff}


class ReconciliationStatus:
    """SQLite-backed status provider consumed by the live mandate gate."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        interval_seconds: float,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._interval_seconds = max(interval_seconds, 1.0)
        self._clock = clock or (lambda: datetime.now(UTC))

    def context(self) -> dict[str, object]:
        row = self._conn.execute(
            "SELECT ts, ok, diff_json FROM reconciliations ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"reconciliation_stale": True, "reconciliation_age_ticks": 999}
        ts = _coerce_datetime(row["ts"])
        age_ticks = 999 if ts is None else int(max((_align(self._clock(), ts) - ts).total_seconds(), 0) // self._interval_seconds)
        ok = bool(row["ok"])
        return {
            "reconciliation_stale": (not ok) or age_ticks > 2,
            "reconciliation_age_ticks": age_ticks,
        }


class Reconciler:
    """Poll live brokers, apply incremental fills, and compare broker truth to SQLite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        brokers: Mapping[Venue, LiveBroker | None],
        bus: EventBus | None = None,
        starting_cash: Decimal = Decimal("10000"),
        interval_seconds: float = 300.0,
        live_order_ttl: timedelta = timedelta(minutes=30),
        clock: Clock | None = None,
    ) -> None:
        self.conn = conn
        self.brokers: dict[Venue, LiveBroker] = {
            venue: broker for venue, broker in brokers.items() if broker is not None
        }
        self.bus = bus
        self.starting_cash = starting_cash
        self.interval_seconds = interval_seconds
        self.live_order_ttl = live_order_ttl
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, *, adopt_broker: bool = False) -> ReconciliationRun:
        """Run one order-tracking and broker/mirror reconciliation pass."""

        results: list[ReconciliationResult] = []
        for venue, broker in self.brokers.items():
            open_orders = await broker.open_orders()
            await self._sync_orders(venue, broker, open_orders)
            await self._cancel_expired(venue, broker)
            result = await self._reconcile_positions(venue, broker, adopt_broker=adopt_broker)
            results.append(result)
        return ReconciliationRun(results=results)

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Run immediately, then at the configured cadence until stopped."""

        while stop_event is None or not stop_event.is_set():
            await self.run_once()
            await asyncio.sleep(self.interval_seconds)

    async def _sync_orders(self, venue: Venue, broker: LiveBroker, open_orders: list[object]) -> None:
        seen: set[str] = set()
        for raw in open_orders:
            client_order_id = _string_field(raw, "client_order_id")
            if client_order_id is None:
                continue
            seen.add(client_order_id)
            status = _status(raw)
            broker_order_id = _string_field(raw, "broker_order_id")
            self._upsert_live_order(client_order_id, venue, status, broker_order_id, raw)
            await self._apply_incremental_fill(venue, raw)
        await self._mark_missing_orders_terminal(venue, seen)

    def _upsert_live_order(
        self,
        client_order_id: str,
        venue: Venue,
        status: LiveOrderStatus,
        broker_order_id: str | None,
        raw: object,
    ) -> None:
        now = self.clock().isoformat()
        existing = self.conn.execute(
            "SELECT submitted_at, raw_json FROM live_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        submitted_at = str(existing["submitted_at"]) if existing is not None else now
        raw_json = _merge_raw_json(existing["raw_json"] if existing is not None else None, raw)
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO live_orders
                (client_order_id, broker_order_id, venue, status, submitted_at, last_sync_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (client_order_id, broker_order_id, venue, status, submitted_at, now, raw_json),
            )

    async def _mark_missing_orders_terminal(self, venue: Venue, seen: set[str]) -> None:
        rows = self.conn.execute(
            "SELECT client_order_id FROM live_orders WHERE venue = ? AND status IN ('working', 'partial')",
            (venue,),
        ).fetchall()
        for row in rows:
            client_order_id = str(row["client_order_id"])
            if client_order_id in seen:
                continue
            append_audit(
                "live_order_missing_from_broker",
                {"client_order_id": client_order_id, "venue": venue},
                actor="reconciler",
            )

    async def _apply_incremental_fill(self, venue: Venue, raw: object) -> None:
        order = self._order_for_raw(raw)
        if order is None:
            return
        filled_qty = _decimal_field(raw, "filled_qty", "filled_quantity", "executed_qty", "executed_quantity")
        status = _status(raw)
        if filled_qty is None and status == "filled":
            filled_qty = order.qty
        if filled_qty is None or filled_qty <= Decimal("0"):
            return
        prior, prior_notional = self._recorded_fill_stats(order.order_id)
        if order.asset_type == "option":
            prior_notional = self._recorded_option_fill_notional(order)
        incremental = filled_qty - prior
        if incremental <= Decimal("0"):
            return
        price = _incremental_fill_price(raw, prior, filled_qty, prior_notional)
        if price is None:
            append_audit(
                "live_fill_skipped_missing_price",
                {"order_id": order.order_id, "client_order_id": order.client_order_id, "venue": venue},
                actor="reconciler",
            )
            return
        if order.asset_type == "option":
            price = _option_total_premium(order, price, incremental)
        fill = Fill(
            order_id=order.order_id,
            price=price,
            qty=incremental,
            ts=_coerce_datetime(_field(raw, "filled_at") or _field(raw, "updated_at")) or self.clock(),
            slippage_usd=_decimal_field(raw, "slippage_usd") or Decimal("0"),
            commission_usd=_decimal_field(raw, "commission_usd", "commission") or Decimal("0"),
            venue=venue,
            broker_order_id=_string_field(raw, "broker_order_id"),
        )
        append_fill(self.conn, fill)
        portfolio = load_portfolio(self.conn, starting_cash=self.starting_cash)
        fill_order = order.model_copy(update={"qty": incremental, "venue": venue})
        if order.asset_type == "option":
            updated, updated_options = _apply_option_increment(
                portfolio,
                load_option_positions(self.conn),
                order,
                fill_order,
                fill,
                incremental,
                venue,
            )
            save_positions(self.conn, updated)
            save_option_positions(self.conn, updated_options)
            snapshot = append_equity_snapshot(self.conn, updated, option_positions=updated_options)
        else:
            updated, _ = apply_fill(portfolio, fill_order, fill)
            save_positions(self.conn, updated)
            snapshot = append_equity_snapshot(self.conn, updated, marks={order.symbol: price})
        if status == "filled" or filled_qty >= order.qty:
            update_order_status(self.conn, order.order_id, "filled")
            self._set_live_status(order.client_order_id, "filled")
        else:
            self._set_live_status(order.client_order_id, "partial")
        append_audit(
            "live_order_fill",
            {"order": fill_order, "fill": fill, "client_order_id": order.client_order_id},
            actor="reconciler",
        )
        await self._publish(OrderFilled(order_id=order.order_id, fill=fill))
        await self._publish(EquityUpdated(equity=snapshot.equity, cash=snapshot.cash, day_pnl=snapshot.day_pnl))

    def _order_for_raw(self, raw: object) -> Order | None:
        client_order_id = _string_field(raw, "client_order_id")
        raw_order = _field(raw, "order")
        if raw_order is not None:
            return _coerce_order(raw_order)
        if client_order_id is None:
            return None
        row = self.conn.execute(
            "SELECT raw_json FROM live_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            decoded = cast(object, json.loads(str(row["raw_json"] or "{}")))
        except json.JSONDecodeError:
            return None
        if isinstance(decoded, dict):
            decoded_map = cast(dict[str, object], decoded)
            return _coerce_order(decoded_map.get("order"))
        return None

    def _recorded_fill_stats(self, order_id: str) -> tuple[Decimal, Decimal]:
        rows = self.conn.execute("SELECT price, qty FROM fills WHERE order_id = ?", (order_id,)).fetchall()
        qty = Decimal("0")
        notional = Decimal("0")
        for row in rows:
            fill_qty = Decimal(str(row["qty"]))
            fill_price = Decimal(str(row["price"]))
            qty += fill_qty
            notional += fill_qty * fill_price
        return qty, notional

    def _recorded_option_fill_notional(self, order: Order) -> Decimal:
        if not order.legs:
            return Decimal("0")
        multiplier = Decimal(order.legs[0].contract.multiplier)
        rows = self.conn.execute("SELECT price FROM fills WHERE order_id = ?", (order.order_id,)).fetchall()
        return sum((abs(Decimal(str(row["price"]))) / multiplier for row in rows), Decimal("0"))

    def _set_live_status(self, client_order_id: str, status: LiveOrderStatus) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE live_orders SET status = ?, last_sync_at = ? WHERE client_order_id = ?",
                (status, self.clock().isoformat(), client_order_id),
            )

    async def _cancel_expired(self, venue: Venue, broker: LiveBroker) -> None:
        cutoff = self.clock() - self.live_order_ttl
        rows = self.conn.execute(
            """SELECT client_order_id, submitted_at FROM live_orders
            WHERE venue = ? AND status IN ('working', 'partial')""",
            (venue,),
        ).fetchall()
        for row in rows:
            submitted_at = _coerce_datetime(row["submitted_at"])
            if submitted_at is None or submitted_at > cutoff:
                continue
            client_order_id = str(row["client_order_id"])
            cancelled = await broker.cancel(client_order_id)
            status: LiveOrderStatus = "cancelled" if cancelled else "rejected"
            self._set_live_status(client_order_id, status)
            append_audit(
                "live_order_ttl_cancelled",
                {"client_order_id": client_order_id, "venue": venue, "cancelled": cancelled},
                actor="reconciler",
            )

    async def _reconcile_positions(
        self,
        venue: Venue,
        broker: LiveBroker,
        *,
        adopt_broker: bool,
    ) -> ReconciliationResult:
        raw_positions = await broker.positions()
        broker_positions = _broker_position_map(raw_positions)
        broker_cash = await _broker_cash(broker, raw_positions)
        if adopt_broker:
            self._adopt_broker_state(venue, broker_positions, broker_cash)
        diff = self._position_diff(broker_positions, broker_cash)
        ok = not diff
        with self.conn:
            self.conn.execute(
                "INSERT INTO reconciliations (ts, venue, ok, diff_json) VALUES (?, ?, ?, ?)",
                (self.clock().isoformat(), venue, 1 if ok else 0, json.dumps(diff, sort_keys=True)),
            )
        if ok:
            append_audit("reconciliation_ok", {"venue": venue}, actor="reconciler")
        else:
            append_audit("reconciliation_failed", {"venue": venue, "diff": diff}, actor="reconciler")
            await self._publish(ReconciliationFailed(venue=venue, diff=diff))
        return ReconciliationResult(venue=venue, ok=ok, diff=diff)

    def _position_diff(
        self,
        broker_positions: Mapping[str, Decimal],
        broker_cash: Decimal | None,
    ) -> dict[str, object]:
        portfolio = load_portfolio(self.conn, starting_cash=self.starting_cash)
        mirror = {position.symbol: position.qty for position in portfolio.positions if position.qty != 0}
        diff: dict[str, object] = {}
        position_diffs: dict[str, dict[str, str]] = {}
        for symbol in sorted(set(mirror) | set(broker_positions)):
            broker_qty = broker_positions.get(symbol, Decimal("0"))
            mirror_qty = mirror.get(symbol, Decimal("0"))
            if broker_qty != mirror_qty:
                position_diffs[symbol] = {"broker_qty": str(broker_qty), "mirror_qty": str(mirror_qty)}
        if position_diffs:
            diff["positions"] = position_diffs
        if broker_cash is not None and abs(broker_cash - portfolio.cash) > Decimal("0.01"):
            diff["cash"] = {"broker_cash": str(broker_cash), "mirror_cash": str(portfolio.cash)}
        return diff

    def _adopt_broker_state(
        self,
        venue: Venue,
        broker_positions: Mapping[str, Decimal],
        broker_cash: Decimal | None,
    ) -> None:
        positions = [
            Position(
                symbol=symbol,
                qty=qty,
                avg_cost=Decimal("0"),
                stop_loss_pct=None,
                take_profit_pct=None,
                opened_at=self.clock(),
                horizon_days=None,
                source_run_id=None,
            )
            for symbol, qty in sorted(broker_positions.items())
            if qty != 0
        ]
        portfolio = Portfolio(cash=broker_cash or Decimal("0"), positions=positions)
        save_positions(self.conn, portfolio)
        append_equity_snapshot(self.conn, portfolio, ts=self.clock())
        append_audit(
            "reconciliation_adopt_broker",
            {"venue": venue, "positions": broker_positions, "cash": broker_cash},
            actor="reconciler",
        )

    async def _publish(self, event: object) -> None:
        if self.bus is not None:
            await self.bus.publish(event)  # type: ignore[arg-type]


def _incremental_fill_price(
    raw: object,
    prior_qty: Decimal,
    filled_qty: Decimal,
    prior_notional: Decimal,
) -> Decimal | None:
    execution_price = _execution_slice_price(raw, prior_qty, filled_qty)
    if execution_price is not None:
        return execution_price
    cumulative_avg = _decimal_field(
        raw, "avg_fill_price", "average_fill_price", "fill_price", "price", "avg_price"
    )
    if cumulative_avg is None:
        return None
    incremental = filled_qty - prior_qty
    if incremental <= Decimal("0"):
        return None
    if prior_qty <= Decimal("0"):
        return cumulative_avg
    return ((filled_qty * cumulative_avg) - prior_notional) / incremental


def _execution_slice_price(raw: object, prior_qty: Decimal, filled_qty: Decimal) -> Decimal | None:
    executions = _field(raw, "executions") or _field(raw, "fills")
    if not isinstance(executions, list):
        return None
    cursor = Decimal("0")
    slice_qty = Decimal("0")
    slice_notional = Decimal("0")
    for execution in cast(list[object], executions):
        qty = _decimal_field(execution, "qty", "quantity", "filled_qty", "filled_quantity")
        price = _decimal_field(execution, "price", "fill_price", "avg_fill_price", "average_price")
        if qty is None or price is None or qty <= Decimal("0"):
            continue
        start = cursor
        end = cursor + qty
        overlap = min(end, filled_qty) - max(start, prior_qty)
        if overlap > Decimal("0"):
            slice_qty += overlap
            slice_notional += overlap * price
        cursor = end
        if cursor >= filled_qty:
            break
    incremental = filled_qty - prior_qty
    if slice_qty == incremental and slice_qty > Decimal("0"):
        return slice_notional / slice_qty
    return None


def _option_total_premium(order: Order, per_contract_price: Decimal, contracts: Decimal) -> Decimal:
    if not order.legs:
        return per_contract_price
    premium = abs(per_contract_price) * contracts * Decimal(order.legs[0].contract.multiplier)
    if order.type == "net_credit":
        return -premium
    if per_contract_price < Decimal("0") and order.type != "net_debit":
        return -premium
    return premium


def _apply_option_increment(
    portfolio: Portfolio,
    option_positions: list[OptionPosition],
    original_order: Order,
    fill_order: Order,
    fill: Fill,
    incremental: Decimal,
    venue: Venue,
) -> tuple[Portfolio, list[OptionPosition]]:
    scaled_legs = _scale_option_legs(original_order.legs or [], incremental, original_order.qty)
    fill_order = fill_order.model_copy(update={"legs": scaled_legs})
    existing_index = _matching_option_position_index(option_positions, original_order.order_id, scaled_legs)
    if existing_index is None:
        updated, updated_options, _ = apply_option_fill(portfolio, option_positions, fill_order, fill)
        return (
            updated,
            [
                position.model_copy(update={"venue": venue})
                if position.position_id == original_order.order_id
                else position
                for position in updated_options
            ],
        )

    scratch_portfolio = portfolio.model_copy(update={"cash": Decimal("1000000000000")})
    _, incremental_positions, _ = apply_option_fill(scratch_portfolio, [], fill_order, fill)
    incremental_position = incremental_positions[0]
    positions = [position.model_copy(deep=True) for position in option_positions]
    existing = positions[existing_index]
    positions[existing_index] = existing.model_copy(
        update={
            "legs": _merge_option_legs(existing.legs, scaled_legs),
            "open_premium": existing.open_premium + fill.price,
            "max_loss": existing.max_loss + incremental_position.max_loss,
            "collateral": existing.collateral + incremental_position.collateral,
            "venue": venue,
        }
    )
    updated = Portfolio(
        cash=portfolio.cash - fill.price - fill.commission_usd,
        positions=[position.model_copy() for position in portfolio.positions],
        day_pnl=portfolio.day_pnl - fill.commission_usd,
    )
    return updated, positions


def _scale_option_legs(legs: list[OptionLeg], incremental: Decimal, total: Decimal) -> list[OptionLeg]:
    if total <= Decimal("0"):
        return [leg.model_copy(deep=True) for leg in legs]
    scaled: list[OptionLeg] = []
    for leg in legs:
        contracts = (Decimal(leg.contracts) * incremental / total).to_integral_value()
        scaled.append(leg.model_copy(update={"contracts": int(contracts)}, deep=True))
    return scaled


def _matching_option_position_index(
    positions: list[OptionPosition], order_id: str, legs: list[OptionLeg]
) -> int | None:
    target = sorted(_option_leg_identity(leg) for leg in legs)
    for index, position in enumerate(positions):
        if position.position_id != order_id:
            continue
        if sorted(_option_leg_identity(leg) for leg in position.legs) == target:
            return index
    return None


def _merge_option_legs(existing: list[OptionLeg], incoming: list[OptionLeg]) -> list[OptionLeg]:
    merged = [leg.model_copy(deep=True) for leg in existing]
    for leg in incoming:
        for index, current in enumerate(merged):
            if _option_leg_identity(current) == _option_leg_identity(leg):
                merged[index] = current.model_copy(update={"contracts": current.contracts + leg.contracts})
                break
        else:
            merged.append(leg.model_copy(deep=True))
    return merged


def _option_leg_identity(leg: OptionLeg) -> tuple[str, str]:
    return (leg.contract.contract_symbol, leg.side)


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(name)
    return getattr(value, name, None)


def _string_field(value: object, name: str) -> str | None:
    item = _field(value, name)
    return str(item) if item is not None else None


def _decimal_field(value: object, *names: str) -> Decimal | None:
    for name in names:
        parsed = _coerce_decimal(_field(value, name))
        if parsed is not None:
            return parsed
    return None


def _coerce_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _status(value: object) -> LiveOrderStatus:
    raw = str(_field(value, "status") or "working").lower()
    if raw in {"filled", "fill", "complete", "completed"}:
        return "filled"
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
    if raw in {"rejected", "failed"}:
        return "rejected"
    if raw in {"partial", "partially_filled", "partially filled"}:
        return "partial"
    filled = _decimal_field(value, "filled_qty", "filled_quantity", "executed_qty", "executed_quantity")
    qty = _decimal_field(value, "qty", "quantity")
    if filled is not None and filled > 0 and (qty is None or filled < qty):
        return "partial"
    return "working"


def _merge_raw_json(existing: object, raw: object) -> str:
    base: dict[str, object] = {}
    if isinstance(existing, str):
        try:
            decoded = json.loads(existing)
            if isinstance(decoded, dict):
                base.update(cast(dict[str, object], decoded))
        except json.JSONDecodeError:
            pass
    base["broker"] = _jsonable(raw)
    order = _field(raw, "order")
    if order is not None:
        base["order"] = _jsonable(order)
    return json.dumps(base, sort_keys=True, separators=(",", ":"))


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return,attr-defined]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in cast(Mapping[object, object], value).items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, Decimal | datetime):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _coerce_order(value: object) -> Order | None:
    if isinstance(value, Order):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, Mapping):
        try:
            return Order.model_validate(dict(cast(Mapping[str, object], value)))
        except Exception:
            return None
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _align(now: datetime, ts: datetime) -> datetime:
    if now.tzinfo is not None and ts.tzinfo is None:
        return now.replace(tzinfo=None)
    if now.tzinfo is None and ts.tzinfo is not None:
        return now.replace(tzinfo=UTC)
    return now


def _broker_position_map(raw_positions: object) -> dict[str, Decimal]:
    positions = cast(Mapping[str, object], raw_positions).get("positions") if isinstance(raw_positions, Mapping) else None
    items: list[object]
    if isinstance(positions, list):
        items = cast(list[object], positions)
    elif isinstance(raw_positions, list):
        items = cast(list[object], raw_positions)
    else:
        items = []
    result: dict[str, Decimal] = {}
    for item in items:
        symbol = _string_field(item, "symbol")
        if symbol is None:
            asset = _string_field(item, "asset_code") or _string_field(item, "asset")
            if asset is not None:
                asset_symbol = asset.upper()
                symbol = asset_symbol if "-" in asset_symbol else f"{asset_symbol}-USD"
        if symbol is None or symbol.upper() == "CASH":
            continue
        qty = _decimal_field(item, "qty", "quantity", "contracts")
        if qty is not None:
            result[symbol] = qty
    return result


async def _broker_cash(broker: LiveBroker, raw_positions: object) -> Decimal | None:
    for key in ("cash", "cash_usd", "buying_power"):
        value = _coerce_decimal(_field(raw_positions, key))
        if value is not None:
            return value
    if isinstance(raw_positions, list):
        for item in cast(list[object], raw_positions):
            if _string_field(item, "symbol") == "CASH":
                value = _decimal_field(item, "cash", "qty", "quantity")
                if value is not None:
                    return value
    for name in ("cash", "cash_balance", "account_cash"):
        member = getattr(broker, name, None)
        if member is None:
            continue
        value = member() if callable(member) else member
        if inspect.isawaitable(value):
            value = await cast(Awaitable[object], value)
        parsed = _coerce_decimal(value)
        if parsed is not None:
            return parsed
    return None

"""File-based Sentinel kill switch."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from sentinel.core.events import KillEngaged, KillSwitchChanged
from sentinel.core.models import Order
from sentinel.execution.broker import LiveBroker
from sentinel.risk._events import emit_event
from sentinel.risk.audit import append_audit
from sentinel.store.db import sentinel_home

FLATTEN_CONFIRMATION = "I UNDERSTAND FLATTEN LIVE POSITIONS"


class LiveRouterLike(Protocol):
    """Tiny duck-typed surface accepted for kill-cancel injection."""

    def live_brokers(self) -> Mapping[str, LiveBroker]: ...


def kill_path() -> Path:
    """Return the kill-switch sentinel file path."""

    return sentinel_home() / "KILL"


def is_engaged() -> bool:
    """Return whether the kill switch is currently engaged."""

    return kill_path().exists()


def engage(
    actor: str = "system",
    brokers: Mapping[str, LiveBroker] | None = None,
    router: LiveRouterLike | None = None,
    flatten: bool = False,
    flatten_confirmation: str | None = None,
) -> None:
    """Engage the kill switch by creating the sentinel file and auditing it."""

    if flatten and flatten_confirmation != FLATTEN_CONFIRMATION:
        raise ValueError(f'flatten requires confirmation: "{FLATTEN_CONFIRMATION}"')
    _write_kill_file(actor)
    cancel_results: dict[str, object] = {}
    flatten_results: dict[str, object] = {}
    live_brokers = _resolve_brokers(brokers, router)
    if live_brokers:
        kill_results = _run_async(_cancel_and_maybe_flatten(live_brokers, flatten))
        cancel_results = kill_results["cancel_results"]
        flatten_results = kill_results["flatten_results"]
    _record_engaged(actor, cancel_results, flatten, flatten_results)


async def engage_async(
    actor: str = "system",
    brokers: Mapping[str, LiveBroker] | None = None,
    router: LiveRouterLike | None = None,
    flatten: bool = False,
    flatten_confirmation: str | None = None,
) -> None:
    """Async-safe kill switch engagement for callers already inside an event loop."""

    if flatten and flatten_confirmation != FLATTEN_CONFIRMATION:
        raise ValueError(f'flatten requires confirmation: "{FLATTEN_CONFIRMATION}"')
    _write_kill_file(actor)
    cancel_results: dict[str, object] = {}
    flatten_results: dict[str, object] = {}
    live_brokers = _resolve_brokers(brokers, router)
    if live_brokers:
        kill_results = await _cancel_and_maybe_flatten(live_brokers, flatten)
        cancel_results = kill_results["cancel_results"]
        flatten_results = kill_results["flatten_results"]
    _record_engaged(actor, cancel_results, flatten, flatten_results)


def _write_kill_file(actor: str) -> None:
    path = kill_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"engaged_by={actor}\nts={datetime.now(UTC).isoformat()}\n", encoding="utf-8")


def _record_engaged(
    actor: str,
    cancel_results: dict[str, object],
    flatten: bool,
    flatten_results: dict[str, object],
) -> None:
    append_audit("kill_switch_changed", {"enabled": True}, actor=actor)
    append_audit(
        "kill_engaged",
        {
            "cancel_results": cancel_results,
            "flatten": flatten,
            "flatten_results": flatten_results,
        },
        actor=actor,
    )
    emit_event(KillSwitchChanged(enabled=True, actor=actor))
    emit_event(
        KillEngaged(
            actor=actor,
            cancel_results=cancel_results,
            flatten_results=flatten_results,
        )
    )


def disengage(actor: str = "system") -> None:
    """Disengage the kill switch by removing the sentinel file and auditing it."""

    path = kill_path()
    if path.exists():
        path.unlink()
    append_audit("kill_switch_changed", {"enabled": False}, actor=actor)
    emit_event(KillSwitchChanged(enabled=False, actor=actor))


async def _cancel_and_maybe_flatten(
    brokers: Mapping[str, LiveBroker],
    flatten: bool,
) -> dict[str, dict[str, object]]:
    cancel_results: dict[str, object] = {}
    flatten_results: dict[str, object] = {}
    for venue, broker in brokers.items():
        cancel_results[venue] = await _cancel_open_orders(broker)
        if flatten:
            flatten_results[venue] = await _flatten_positions(broker, venue)
    return {"cancel_results": cancel_results, "flatten_results": flatten_results}


async def _cancel_open_orders(broker: LiveBroker) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    try:
        open_orders = await broker.open_orders()
    except Exception as exc:
        return [{"ok": False, "error": str(exc)}]
    for open_order in open_orders:
        client_order_id = _extract_text(open_order, "client_order_id", "order_id", "id")
        if client_order_id is None:
            results.append({"ok": False, "error": "open order missing client_order_id"})
            continue
        try:
            ok = await broker.cancel(client_order_id)
        except Exception as exc:
            results.append({"client_order_id": client_order_id, "ok": False, "error": str(exc)})
        else:
            results.append({"client_order_id": client_order_id, "ok": ok})
    return results


async def _flatten_positions(broker: LiveBroker, venue: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    try:
        positions = await broker.positions()
    except Exception as exc:
        return [{"ok": False, "error": str(exc)}]
    for position in positions:
        order = _flatten_order(position, venue)
        if order is None:
            results.append({"ok": False, "error": "position missing symbol or quantity"})
            continue
        try:
            result = await broker.submit(order)
        except Exception as exc:
            results.append({"symbol": order.symbol, "ok": False, "error": str(exc)})
        else:
            results.append({"symbol": order.symbol, "ok": True, "result": str(result)})
    return results


def _flatten_order(position: object, venue: str) -> Order | None:
    symbol = _extract_text(position, "symbol")
    qty = _extract_decimal(position, "qty", "quantity")
    if symbol is None or qty is None or qty == 0:
        return None
    asset_type = "crypto" if venue == "robinhood_crypto" else "equity"
    return Order(
        order_id=f"flatten-{symbol}-{datetime.now(UTC).timestamp()}",
        run_id=None,
        symbol=symbol,
        side="sell" if qty > 0 else "buy",
        qty=abs(qty),
        type="market",
        reason="manual",
        created_at=datetime.now(UTC),
        asset_type=asset_type,  # type: ignore[arg-type]
        venue=venue,  # type: ignore[arg-type]
    )


def _resolve_brokers(
    brokers: Mapping[str, LiveBroker] | None,
    router: LiveRouterLike | None,
) -> Mapping[str, LiveBroker]:
    if brokers is not None:
        return brokers
    if router is not None:
        return router.live_brokers()
    return {}


def _extract_text(value: object, *names: str) -> str | None:
    for name in names:
        raw = _extract_value(value, name)
        if raw is not None:
            return str(raw)
    return None


def _extract_decimal(value: object, *names: str) -> Decimal | None:
    for name in names:
        raw = _extract_value(value, name)
        if raw is not None:
            return Decimal(str(raw))
    return None


def _extract_value(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        value_map = cast(Mapping[str, object], value)
        return value_map.get(name)
    return getattr(value, name, None)


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    msg = "killswitch engage with live brokers cannot run inside an active event loop"
    raise RuntimeError(msg)

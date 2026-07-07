"""Robinhood Agentic Trading MCP live broker."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sentinel.config.settings import RobinhoodAgenticSettings
from sentinel.core.models import Fill, OptionLeg, Order
from sentinel.execution.broker import LiveBroker, OrderPending, OrderRejected
from sentinel.risk.audit import AuditRecord, append_audit

JsonObject = dict[str, object]
AuditCallable = Callable[[str, Mapping[str, object] | None, str], AuditRecord]

_FILLED = {"filled", "executed", "complete", "completed"}
_APPROVAL_PENDING = {"approval_pending", "pending_approval", "requires_approval"}
_WORKING = {"open", "working", "queued", "submitted", "pending", *_APPROVAL_PENDING}


class RobinhoodAgenticBroker(LiveBroker):
    """Official Robinhood Agentic Trading MCP connector for live equities/options."""

    def __init__(
        self,
        settings: RobinhoodAgenticSettings | None = None,
        *,
        session: object | None = None,
        audit: AuditCallable | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or RobinhoodAgenticSettings()
        if not self.settings.enabled:
            raise RuntimeError("RobinhoodAgenticBroker is disabled")
        self._session = session if session is not None else _build_mcp_session(self.settings)
        self._audit = audit or append_audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._capabilities: set[str] = set()
        self._tools: dict[str, str] = {}
        self._probe_if_sync()

    async def probe(self) -> set[str]:
        """Probe MCP tools and cache advertised Sentinel capabilities."""

        tools = await _maybe_await(_call_no_args(self._session, "list_tools"))
        names = _tool_names(tools)
        self._tools = _select_tools(names)
        caps: set[str] = set()
        if _has_equity_tools(names):
            caps.add("equity")
        if any("option" in name.lower() for name in names):
            caps.add("option")
        self._capabilities = caps
        return set(caps)

    def capabilities(self) -> set[str]:
        """Return capabilities learned from the runtime MCP tool probe."""

        return set(self._capabilities)

    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending:
        """Preview then submit a Sentinel order through Robinhood MCP tools."""

        await self._ensure_probe()
        if order.asset_type not in self._capabilities:
            return OrderRejected(
                reason=f"robinhood agentic does not advertise capability {order.asset_type}",
                code="VENUE_CAPABILITY_MISSING",
            )
        if order.asset_type == "option" and not order.legs:
            return OrderRejected(reason="option order requires legs", code="VENUE_CAPABILITY_MISSING")

        existing = await self._lookup_order(order.client_order_id)
        if existing is not None:
            return self._result_from_response(existing, order)

        payload = self._trade_payload(order)
        preview_tool = self._tool_for("option_preview" if order.asset_type == "option" else "equity_preview")
        submit_tool = self._tool_for("option_submit" if order.asset_type == "option" else "equity_submit")
        if preview_tool is None or submit_tool is None:
            return OrderRejected(
                reason=f"robinhood agentic missing trade tools for {order.asset_type}",
                code="VENUE_CAPABILITY_MISSING",
            )

        preview = await self._call_tool(preview_tool, payload)
        self._audit(
            "robinhood_agentic_trade_preview",
            {"client_order_id": order.client_order_id, "venue": "robinhood_agentic", "preview": preview},
            "robinhood_agentic",
        )
        submitted = await self._call_tool(submit_tool, {**payload, "preview_id": preview.get("preview_id")})
        return self._result_from_response(submitted, order)

    async def cancel(self, client_order_id: str) -> bool:
        """Cancel a working MCP order."""

        await self._ensure_probe()
        tool = self._tool_for("cancel")
        if tool is None:
            return False
        result = await self._call_tool(tool, {"client_order_id": client_order_id})
        return bool(result.get("ok", True))

    async def open_orders(self) -> list[object]:
        """Return RH account orders for reconciliation/live panel use only."""

        await self._ensure_probe()
        tool = self._tool_for("orders")
        if tool is None:
            return []
        result = await self._call_tool(tool, {})
        return [
            {**item, "venue": "robinhood_agentic"}
            for item in _items(result)
            if _status(item) in _WORKING
        ]

    async def positions(self) -> list[object]:
        """Return RH account positions for reconciliation/live panel use only."""

        await self._ensure_probe()
        tool = self._tool_for("positions")
        if tool is None:
            return []
        result = await self._call_tool(tool, {})
        return [{**item, "venue": "robinhood_agentic"} for item in _items(result)]

    async def _ensure_probe(self) -> None:
        if not self._tools:
            await self.probe()

    def _probe_if_sync(self) -> None:
        try:
            value = _call_no_args(self._session, "list_tools")
        except Exception:
            return
        if inspect.isawaitable(value):
            close = getattr(value, "close", None)
            if callable(close):
                close()
            return
        names = _tool_names(value)
        self._tools = _select_tools(names)
        self._capabilities = {"equity"} if _has_equity_tools(names) else set()
        if any("option" in name.lower() for name in names):
            self._capabilities.add("option")

    def _tool_for(self, role: str) -> str | None:
        return self._tools.get(role)

    async def _call_tool(self, tool_name: str, arguments: Mapping[str, object]) -> JsonObject:
        caller = getattr(self._session, "call_tool", None) or getattr(self._session, "tool_call", None)
        if caller is None:
            raise RuntimeError("Robinhood Agentic MCP session must expose call_tool()")
        try:
            result = caller(tool_name, dict(arguments))
        except TypeError:
            result = caller(name=tool_name, arguments=dict(arguments))
        decoded = await _maybe_await(result)
        return _as_object(decoded)

    async def _lookup_order(self, client_order_id: str) -> JsonObject | None:
        tool = self._tool_for("orders")
        if tool is None:
            return None
        result = await self._call_tool(tool, {"client_order_id": client_order_id})
        for item in _items(result):
            if _string_field(item, "client_order_id") == client_order_id:
                return item
        return None

    def _trade_payload(self, order: Order) -> JsonObject:
        base: JsonObject = {
            "client_order_id": order.client_order_id,
            "asset_type": order.asset_type,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": str(order.qty),
            "order_type": order.type,
        }
        if order.asset_type == "option":
            base["strategy"] = order.strategy
            base["legs"] = [_map_option_leg(leg) for leg in order.legs or []]
        return base

    def _result_from_response(self, raw: Mapping[str, object], order: Order) -> Fill | OrderPending:
        status = _status(raw)
        broker_order_id = _string_field(raw, "broker_order_id", "order_id", "id")
        if status in _FILLED or raw.get("fill") is not None:
            fill_raw = raw.get("fill") if isinstance(raw.get("fill"), dict) else raw
            fill = cast(Mapping[str, object], fill_raw)
            qty = _decimal_field(fill, "qty", "quantity", "filled_qty") or order.qty
            price = _decimal_field(fill, "price", "avg_fill_price", "average_price") or Decimal("0")
            if order.asset_type == "option":
                price = _option_total_premium(order, price, qty)
            return Fill(
                order_id=order.order_id,
                price=price,
                qty=qty,
                ts=self._clock(),
                slippage_usd=Decimal("0"),
                commission_usd=_decimal_field(fill, "commission", "commission_usd") or Decimal("0"),
                venue="robinhood_agentic",
                broker_order_id=broker_order_id,
            )
        return OrderPending(
            client_order_id=order.client_order_id,
            venue="robinhood_agentic",
            broker_order_id=broker_order_id,
        )


def _option_total_premium(order: Order, per_contract_price: Decimal, contracts: Decimal) -> Decimal:
    if not order.legs:
        return per_contract_price
    premium = abs(per_contract_price) * contracts * Decimal(order.legs[0].contract.multiplier)
    if order.type == "net_credit":
        return -premium
    if per_contract_price < Decimal("0") and order.type != "net_debit":
        return -premium
    return premium


def _build_mcp_session(settings: RobinhoodAgenticSettings) -> object:
    endpoint = os.environ.get(settings.mcp_endpoint_env)
    if not endpoint:
        raise RuntimeError("Robinhood Agentic MCP endpoint environment variable is required")
    raise RuntimeError("Inject an MCP session for Robinhood Agentic trading")


def _call_no_args(target: object, name: str) -> object:
    method = getattr(target, name)
    return method()


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _tool_names(raw: object) -> list[str]:
    if isinstance(raw, Mapping):
        mapping = cast(Mapping[str, object], raw)
        tools = mapping.get("tools") or mapping.get("results") or mapping.get("data")
    else:
        tools = raw
    if not isinstance(tools, Sequence) or isinstance(tools, str | bytes | bytearray):
        return []
    names: list[str] = []
    for item in cast(Sequence[object], tools):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, Mapping):
            item_map = cast(Mapping[str, object], item)
            name = item_map.get("name")
            if name is not None:
                names.append(str(name))
    return names


def _select_tools(names: Sequence[str]) -> dict[str, str]:
    lowered = {name: name.lower() for name in names}
    selected: dict[str, str | None] = {}
    selected["equity_preview"] = _find(lowered, "preview", "equity") or _find(lowered, "preview", "stock")
    selected["equity_submit"] = (
        _find(lowered, "submit", "equity")
        or _find(lowered, "place", "equity")
        or _find(lowered, "trade", "equity")
        or _find(lowered, "submit", "stock")
    )
    selected["option_preview"] = _find(lowered, "preview", "option")
    selected["option_submit"] = (
        _find(lowered, "submit", "option")
        or _find(lowered, "place", "option")
        or _find(lowered, "trade", "option")
    )
    selected["cancel"] = _find(lowered, "cancel")
    selected["orders"] = _find(lowered, "order", "list") or _find(lowered, "orders")
    selected["positions"] = _find(lowered, "position")
    return {key: value for key, value in selected.items() if value is not None}


def _find(lowered: Mapping[str, str], *needles: str) -> str | None:
    for original, value in lowered.items():
        if all(needle in value for needle in needles):
            return original
    return None


def _has_equity_tools(names: Sequence[str]) -> bool:
    lowered = [name.lower() for name in names]
    return any(("equity" in name or "stock" in name) and "preview" in name for name in lowered)


def _map_option_leg(leg: OptionLeg) -> JsonObject:
    return {
        "contract_symbol": leg.contract.contract_symbol,
        "underlying": leg.contract.underlying,
        "option_type": leg.contract.kind,
        "strike": str(leg.contract.strike),
        "expiry": leg.contract.expiry.isoformat(),
        "side": leg.side,
        "quantity": leg.contracts,
        "limit_price": str(leg.limit_price) if leg.limit_price is not None else None,
    }


def _as_object(value: object) -> JsonObject:
    if isinstance(value, Mapping):
        value_map = cast(Mapping[str, object], value)
        content = value_map.get("content")
        if isinstance(content, list) and content and isinstance(content[0], Mapping):
            nested = cast(Mapping[str, object], content[0])
            if isinstance(nested.get("json"), Mapping):
                return dict(cast(Mapping[str, object], nested["json"]))
        return dict(value_map)
    return {}


def _items(raw: Mapping[str, object]) -> list[JsonObject]:
    value = raw.get("orders") or raw.get("positions") or raw.get("results") or raw.get("data")
    if isinstance(value, list):
        return [
            dict(cast(Mapping[str, object], item))
            for item in cast(list[object], value)
            if isinstance(item, Mapping)
        ]
    return [dict(raw)]


def _status(raw: Mapping[str, object]) -> str:
    value = _string_field(raw, "status", "state") or "working"
    return value.lower()


def _string_field(raw: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = raw.get(name)
        if value is not None:
            return str(value)
    return None


def _decimal_field(raw: Mapping[str, object], *names: str) -> Decimal | None:
    for name in names:
        value = raw.get(name)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None

"""Deterministic execution routing across paper and staged live venues."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from sentinel.core.models import Fill, GateResult, Order, Violation, ViolationCode
from sentinel.execution.broker import Broker, LiveBroker, OrderPending, OrderRejected, Venue
from sentinel.risk.audit import AuditRecord, append_audit

AuditCallable = Callable[[str, Mapping[str, object] | None, str], AuditRecord]
ReconciliationContextProvider = Callable[[], Mapping[str, object]]

_LIVE_VENUE_BY_ASSET: dict[str, Venue] = {
    "crypto": "robinhood_crypto",
    "equity": "robinhood_agentic",
    "option": "robinhood_agentic",
}
_STAGE_FLAG_BY_ASSET = {
    "crypto": "crypto_stage_enabled",
    "equity": "equity_stage_enabled",
    "option": "options_stage_enabled",
}
_LIVE_BROKER_BY_VENUE = {
    "robinhood_crypto": "crypto",
    "robinhood_agentic": "agentic",
}
_SECRET_WORDS = ("key", "secret", "token", "password", "signature", "credential")


class LiveMandateCheck(Protocol):
    """Duck-typed live mandate overlay supplied by WS12."""

    def check_live_order(self, order: Order, **context: object) -> GateResult: ...


class ExecutionRouter:
    """Route orders by asset class and enabled live stage without paper fallthrough."""

    def __init__(
        self,
        paper: Broker,
        crypto: LiveBroker | None = None,
        agentic: LiveBroker | None = None,
        live_mandate: LiveMandateCheck | object | None = None,
        audit: AuditCallable | None = None,
        reconciliation_context: ReconciliationContextProvider | None = None,
    ) -> None:
        self._paper = paper
        self._crypto = crypto
        self._agentic = agentic
        self._live_mandate = live_mandate
        self._audit = audit
        self._reconciliation_context = reconciliation_context

    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending:
        """Submit to the deterministic target venue for this order."""

        target = self._target_venue(order)
        routed_order = _with_venue(order, target)
        if target == "paper":
            return await self._paper.submit(routed_order)

        if self._live_mandate is None:
            return _rejection(
                "LIVE_STAGE_NOT_ENABLED",
                f"live stage for {order.asset_type} is not enabled",
            )

        broker = self._live_broker(target)
        if broker is None:
            return _rejection(
                "VENUE_CAPABILITY_MISSING",
                f"no live broker configured for {target}",
            )
        capabilities = broker.capabilities()
        if order.asset_type not in capabilities:
            return _rejection(
                "VENUE_CAPABILITY_MISSING",
                f"{target} does not advertise capability {order.asset_type}",
            )

        gate_context: dict[str, object] = {"venue": target}
        if self._reconciliation_context is not None:
            gate_context.update(self._reconciliation_context())
        gate = _check_live_order(self._live_mandate, routed_order, **gate_context)
        if not gate.passed:
            return _gate_rejection(gate)

        self._audit_live("live_order_submit", {"order": routed_order, "venue": target})
        existing = await _find_pending_order(broker, routed_order)
        if existing is not None:
            self._audit_live(
                "live_order_idempotent_requery",
                {"order": routed_order, "venue": target, "result": existing},
            )
            return existing
        try:
            result = await broker.submit(routed_order)
        except Exception as exc:
            existing = await _find_pending_order(broker, routed_order)
            if existing is not None:
                self._audit_live(
                    "live_order_idempotent_requery",
                    {"order": routed_order, "venue": target, "result": existing},
                )
                return existing
            rejection = OrderRejected(reason=f"live broker submit failed: {exc}")
            self._audit_live(
                "live_order_rejected",
                {"order": routed_order, "venue": target, "rejection": rejection},
            )
            return rejection

        self._audit_live("live_order_ack", {"order": routed_order, "venue": target, "result": result})
        return result

    def _target_venue(self, order: Order) -> Venue:
        live_venue = _LIVE_VENUE_BY_ASSET[order.asset_type]
        if self._live_mandate is None:
            return live_venue if self._live_broker(live_venue) is not None else "paper"
        if not _stage_enabled(self._live_mandate, order.asset_type):
            return "paper"
        return live_venue

    def _live_broker(self, venue: Venue) -> LiveBroker | None:
        broker_name = _LIVE_BROKER_BY_VENUE.get(venue)
        if broker_name == "crypto":
            return self._crypto
        if broker_name == "agentic":
            return self._agentic
        return None

    def _audit_live(self, kind: str, payload: Mapping[str, object]) -> None:
        if self._audit is None:
            return
        self._audit(kind, _redact_mapping(payload), "execution_router")


def _with_venue(order: Order, venue: Venue) -> Order:
    return order.model_copy(update={"venue": venue})


def _stage_enabled(live_mandate: object, asset_type: str) -> bool:
    flag = _STAGE_FLAG_BY_ASSET[asset_type]
    source = getattr(live_mandate, "live", live_mandate)
    if isinstance(source, Mapping):
        values = cast(Mapping[str, object], source)
        return bool(values.get(flag, False))
    return bool(getattr(source, flag, False))


def _check_live_order(mandate: object, order: Order, **context: object) -> GateResult:
    checker = cast(LiveMandateCheck, mandate).check_live_order
    try:
        result = checker(order, **context)
    except TypeError:
        result = checker(order)
    if inspect.isawaitable(result):
        raise TypeError("LiveMandateCheck.check_live_order must be synchronous")
    return result


async def _find_pending_order(broker: LiveBroker, order: Order) -> OrderPending | None:
    try:
        open_orders = await broker.open_orders()
    except Exception:
        return None
    for raw in open_orders:
        if _field(raw, "client_order_id") != order.client_order_id:
            continue
        venue = cast(Venue, _field(raw, "venue") or order.venue)
        return OrderPending(
            client_order_id=order.client_order_id,
            venue=venue,
            broker_order_id=cast(str | None, _field(raw, "broker_order_id")),
        )
    return None


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(name)
    return getattr(value, name, None)


def _gate_rejection(gate: GateResult) -> OrderRejected:
    first = gate.violations[0] if gate.violations else None
    if first is None:
        return OrderRejected(reason="live mandate rejected order")
    return OrderRejected(reason=first.message, code=first.code)


def _rejection(code: ViolationCode, message: str) -> OrderRejected:
    violation = Violation(code=code, message=message)
    return OrderRejected(reason=violation.message, code=violation.code)


def _redact_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: _redact_value(key, value) for key, value in payload.items()}


def _redact_value(key: str, value: object) -> object:
    lowered = key.lower()
    if any(word in lowered for word in _SECRET_WORDS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return _redact_mapping(cast(Mapping[str, object], value))
    return value


def fsynced_audit(kind: str, payload: Mapping[str, object] | None, actor: str) -> AuditRecord:
    """Adapter for callers that want the built-in fsynced audit sink."""

    return append_audit(kind, payload, actor=actor)

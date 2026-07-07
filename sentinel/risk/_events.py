"""Optional event-bus bridge for synchronous risk APIs."""

from __future__ import annotations

import asyncio

from sentinel.core.bus import EventBus
from sentinel.core.events import Event

_event_bus: EventBus | None = None


def set_event_bus(bus: EventBus | None) -> None:
    """Set the process-local event bus used by risk modules."""

    global _event_bus
    _event_bus = bus


def emit_event(event: Event) -> None:
    """Best-effort publish to the configured event bus, if any."""

    bus = _event_bus
    if bus is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(bus.publish(event))
        return
    task = loop.create_task(bus.publish(event))
    task.add_done_callback(lambda completed: completed.exception())

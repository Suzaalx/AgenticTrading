from __future__ import annotations

import asyncio

import pytest

from sentinel.core.bus import EventBus
from sentinel.core.events import LogLine


@pytest.mark.asyncio
async def test_event_bus_fanout() -> None:
    bus = EventBus()
    q1 = await bus.subscribe_queue()
    q2 = await bus.subscribe_queue()
    event = LogLine(level="info", message="hello")
    await bus.publish(event)
    assert await asyncio.wait_for(q1.get(), timeout=1) == event
    assert await asyncio.wait_for(q2.get(), timeout=1) == event
    await bus.unsubscribe(q1)
    await bus.publish(LogLine(level="info", message="bye"))
    assert q1.empty()
    assert (await asyncio.wait_for(q2.get(), timeout=1)).message == "bye"

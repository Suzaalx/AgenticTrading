"""In-process async fanout event bus."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sentinel.core.events import Event


class EventBus:
    """Fan out events to multiple asyncio queue subscribers on one event loop."""

    def __init__(self, max_queue_size: int = 0) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._lock = asyncio.Lock()

    async def subscribe_queue(self) -> asyncio.Queue[Event]:
        """Subscribe and receive a queue that must later be passed to unsubscribe."""

        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Remove a previously subscribed queue."""

        async with self._lock:
            self._subscribers.discard(queue)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        """Context-managed queue subscription."""

        queue = await self.subscribe_queue()
        try:
            yield queue
        finally:
            await self.unsubscribe(queue)

    async def publish(self, event: Event) -> None:
        """Publish an event to all current subscribers."""

        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            await queue.put(event)

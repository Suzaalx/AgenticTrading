"""Internal asyncio cron scheduler for Sentinel decision runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

from croniter import croniter

from sentinel.core.commands import Depth

if TYPE_CHECKING:
    from .service import SentinelCommandService

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class DecisionScheduler:
    """Cron scheduler that fires orchestrator runs for configured watchlist symbols."""

    def __init__(
        self,
        service: SentinelCommandService,
        *,
        cron: str | None = None,
        watchlist: list[str] | None = None,
        depth: Depth = "standard",
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self.service = service
        self.cron = cron or service.settings.schedule.decision_cron
        self.watchlist = watchlist or list(service.settings.schedule.watchlist)
        self.depth: Depth = depth
        self.clock = clock or datetime.now
        self.sleep = sleep or asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._next_fire: datetime | None = None

    def start(self) -> None:
        """Start the scheduler loop; it does not auto-start at construction."""

        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="sentinel-decision-scheduler")

    async def stop(self) -> None:
        """Stop the scheduler loop."""

        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def fire_due(self, now: datetime | None = None) -> list[str]:
        """Fire every due cron occurrence without sleeping; intended for tests."""

        current = now or self.clock()
        fired: list[str] = []
        if self._next_fire is None:
            self._next_fire = croniter(self.cron, current).get_next(datetime)
        while self._next_fire <= current and not self._stopping.is_set():
            fire_time = self._next_fire
            for symbol in self.watchlist:
                fired.append(await self.service.start_run(symbol, fire_time.date(), self.depth))
            self._next_fire = croniter(self.cron, fire_time).get_next(datetime)
        return fired

    async def _loop(self) -> None:
        self._next_fire = croniter(self.cron, self.clock()).get_next(datetime)
        while not self._stopping.is_set():
            if self._next_fire is None:
                self._next_fire = croniter(self.cron, self.clock()).get_next(datetime)
            next_fire = self._next_fire
            delay = max(0.0, (next_fire - self.clock()).total_seconds())
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                break
            except TimeoutError:
                await self.fire_due(next_fire)

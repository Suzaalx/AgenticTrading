from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from sentinel.config.settings import Settings
from sentinel.core.commands import Depth
from sentinel.orchestrator.scheduler import DecisionScheduler


class FakeService:
    def __init__(self) -> None:
        self.settings = Settings()
        self.started: list[tuple[str, date | None, Depth]] = []

    async def start_run(self, symbol: str, run_date: date | None, depth: Depth) -> str:
        self.started.append((symbol, run_date, depth))
        return f"run-{symbol}"


@pytest.mark.asyncio
async def test_scheduler_cron_fire_path_with_fake_clock() -> None:
    service = FakeService()
    base = datetime(2026, 7, 6, 9, 30)
    scheduler = DecisionScheduler(
        service,  # type: ignore[arg-type]
        cron="31 9 * * *",
        watchlist=["NVDA", "AAPL"],
        depth="fast",
        clock=lambda: base,
    )

    assert await scheduler.fire_due(base) == []
    fired = await scheduler.fire_due(base + timedelta(minutes=1))

    assert fired == ["run-NVDA", "run-AAPL"]
    assert service.started == [
        ("NVDA", date(2026, 7, 6), "fast"),
        ("AAPL", date(2026, 7, 6), "fast"),
    ]

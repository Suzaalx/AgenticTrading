"""Command contract shared by CLI and TUI."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Protocol

Depth = Literal["fast", "standard", "deep"]


class CommandService(Protocol):
    """Async command interface implemented by the orchestrator in later phases."""

    async def start_run(self, symbol: str, date: date | None, depth: Depth) -> str: ...
    async def cancel_run(self, run_id: str) -> None: ...
    async def toggle_kill(self, on: bool) -> None: ...
    async def close_position(self, symbol: str) -> None: ...
    async def start_backtest(self, config: dict[str, Any]) -> str: ...
    async def reflect_now(self) -> None: ...


class NullCommandService:
    """Phase 0 stub used by the TUI until the orchestrator exists."""

    async def start_run(self, symbol: str, date: date | None, depth: Depth) -> str:
        raise NotImplementedError("start_run is not implemented in Phase 0")

    async def cancel_run(self, run_id: str) -> None:
        raise NotImplementedError("cancel_run is not implemented in Phase 0")

    async def toggle_kill(self, on: bool) -> None:
        raise NotImplementedError("toggle_kill is not implemented in Phase 0")

    async def close_position(self, symbol: str) -> None:
        raise NotImplementedError("close_position is not implemented in Phase 0")

    async def start_backtest(self, config: dict[str, Any]) -> str:
        raise NotImplementedError("start_backtest is not implemented in Phase 0")

    async def reflect_now(self) -> None:
        raise NotImplementedError("reflect_now is not implemented in Phase 0")

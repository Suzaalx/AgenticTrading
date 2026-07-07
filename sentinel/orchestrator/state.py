"""RunState construction and checkpoint serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sentinel.core.ids import new_run_id
from sentinel.core.models import RunState, RunStatus
from sentinel.store.db import sentinel_home


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def initial_state(
    symbol: str,
    *,
    as_of: datetime | None = None,
    run_id: str | None = None,
    mode: Literal["decision", "backtest_step"] = "decision",
) -> RunState:
    """Construct the canonical pending state for a new decision pipeline run."""

    resolved_as_of = as_of or utc_now()
    if resolved_as_of.tzinfo is None:
        resolved_as_of = resolved_as_of.replace(tzinfo=UTC)
    return RunState(
        run_id=run_id or new_run_id(),
        symbol=symbol.upper(),
        as_of=resolved_as_of,
        mode=mode,
        status="pending",
        snapshot=None,
        investment_plan=None,
        trade_proposal=None,
        pm_decision=None,
        final_order=None,
        error=None,
    )


def transition(state: RunState, status: RunStatus, *, error: str | None = None) -> RunState:
    """Mutate and return ``state`` with a new status and optional error."""

    state.status = status
    if error is not None:
        state.error = error
    return state


def run_dir(run_id: str) -> Path:
    """Return the persistent per-run directory."""

    path = sentinel_home() / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def serialize_state(state: RunState) -> str:
    """Serialize a RunState for durable checkpoint storage."""

    return state.model_dump_json()


def deserialize_state(payload: str) -> RunState:
    """Deserialize a checkpointed RunState."""

    return RunState.model_validate_json(payload)

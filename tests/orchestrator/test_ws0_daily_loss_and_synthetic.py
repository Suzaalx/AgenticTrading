from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.core.bus import EventBus
from sentinel.orchestrator.runner import OrchestratorRunner

from ._helpers import FixtureRouter, conn, fake_llm, mandate, settings


@pytest.mark.asyncio
async def test_daily_loss_halt_vetoes_order_through_gate_path(tmp_path) -> None:
    active = OrchestratorRunner(
        bus=EventBus(),
        conn=conn(tmp_path),
        settings=settings(),
        mandate=mandate(),
        llm=fake_llm(),
        router=FixtureRouter(price=Decimal("50")),  # type: ignore[arg-type]
    )
    active.conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
        ("NVDA", "10", "100", None, None, None, datetime.now(UTC).isoformat(), "prior-run"),
    )
    active.conn.execute(
        "INSERT INTO equity_curve VALUES (?,?,?,?)",
        (datetime.now(UTC).isoformat(), "10000", "9000", "0"),
    )
    active.conn.commit()

    state = await active.run("NVDA")

    assert state.status == "completed"
    assert state.final_order is None
    assert state.error is not None
    assert "DAILY_LOSS_HALT" in state.error
    assert active.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_synthetic_snapshot_allows_analysis_but_refuses_execution(tmp_path) -> None:
    router = FixtureRouter(price=Decimal("100"))
    router.providers_used = {
        "ohlcv": "local_synthetic",
        "ohlcv:NVDA": "local_synthetic",
        "news": "fixture",
        "fundamentals": "fixture",
    }
    active = OrchestratorRunner(
        bus=EventBus(),
        conn=conn(tmp_path),
        settings=settings(),
        mandate=mandate(),
        llm=fake_llm(),
        router=router,  # type: ignore[arg-type]
    )

    state = await active.run("NVDA")

    assert state.snapshot is not None
    assert state.snapshot.data_quality == "synthetic"
    assert state.analyst_reports
    assert state.pm_decision is not None
    assert state.pm_decision.verdict == "APPROVE"
    assert state.final_order is None
    assert state.error == "execution refused: synthetic data quality"
    assert active.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

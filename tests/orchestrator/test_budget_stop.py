from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.llm.budget import BudgetExceeded
from sentinel.orchestrator.runner import OrchestratorRunner

from ._helpers import FixtureRouter, conn, fake_llm, mandate, settings


async def test_over_budget_run_refuses_to_start(tmp_path) -> None:
    active_conn = conn(tmp_path)
    active_conn.execute(
        "INSERT INTO costs (ts, run_id, agent, model, tokens_in, tokens_out, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(UTC).isoformat(), "old", "agent", "model", 1, 1, 1.0),
    )
    active_conn.commit()
    active = OrchestratorRunner(
        conn=active_conn,
        settings=settings(budget=1.0),
        mandate=mandate(),
        llm=fake_llm(),
        router=FixtureRouter(),  # type: ignore[arg-type]
    )

    with pytest.raises(BudgetExceeded):
        await active.run("NVDA")

    assert active_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

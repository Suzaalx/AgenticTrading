from __future__ import annotations

from sentinel.config.settings import LLMSettings
from sentinel.llm.gateway import LLMGateway
from tests.orchestrator._helpers import (
    EventBus,
    FixtureRouter,
    OrchestratorRunner,
    conn,
    fake_llm,
    mandate,
    settings,
)


async def test_full_run_through_real_gateway_meters_tokens_and_latency_per_call(tmp_path) -> None:
    """Free-tier metering: every LLM call (agents *and* debate turns) lands in costs with latency."""

    base = settings()
    open_weight = base.model_copy(
        update={"llm": LLMSettings(provider="groq", quick_model="llama-3.1-8b-instant", max_retries=0)}
    )
    gateway = LLMGateway(settings=open_weight, provider=fake_llm())
    active = OrchestratorRunner(
        bus=EventBus(),
        conn=conn(tmp_path),
        settings=open_weight,
        mandate=mandate(),
        llm=gateway,
        router=FixtureRouter(),  # type: ignore[arg-type]
    )

    state = await active.run("NVDA", depth="standard")

    assert state.status == "completed"
    assert state.final_order is not None
    rows = active.conn.execute(
        "SELECT agent, model, latency_ms, cost_usd FROM costs WHERE run_id = ?", (state.run_id,)
    ).fetchall()
    agents = {row["agent"] for row in rows}
    assert {"market_analyst", "bull_researcher", "bear_researcher", "trader", "portfolio_manager"} <= agents
    assert all(row["latency_ms"] is not None and row["latency_ms"] >= 0 for row in rows)
    assert all(float(row["cost_usd"]) == 0.0 for row in rows)  # free tier: $0 but still metered
    run_row = active.conn.execute("SELECT cost_usd, tokens FROM runs WHERE run_id = ?", (state.run_id,)).fetchone()
    assert float(run_row["cost_usd"]) == 0.0

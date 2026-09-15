from __future__ import annotations

from sentinel.agents.analyst_rollup import DEBATE_DISABLED_SCORECARD, ROLLUP_AGENT_NAME
from sentinel.store.repos.runs_repo import get_run
from sentinel.testing.fakes import FakeLLM

from ._helpers import fake_llm, runner

DEBATE_AGENTS = {"bull_researcher", "bear_researcher", "debate_convergence_classifier", "research_manager"}


def _llm_without_debate_roles() -> FakeLLM:
    """A FakeLLM that raises KeyError if any debate role is invoked (bypass must be total)."""

    llm = fake_llm()
    for name in DEBATE_AGENTS:
        llm.responses.pop(name)
    return llm


async def test_debate_off_routes_analysts_straight_to_trader(tmp_path) -> None:
    llm = _llm_without_debate_roles()
    active = runner(tmp_path, llm=llm, debate_enabled=False)

    state = await active.run("NVDA", depth="standard")

    assert state.status == "completed"
    assert state.debate_enabled is False
    assert state.debate_transcript == []
    assert not DEBATE_AGENTS & {call.agent for call in llm.calls}
    # Deterministic roll-up stands in for the research manager's plan.
    assert state.investment_plan is not None
    assert state.investment_plan.agent == ROLLUP_AGENT_NAME
    assert state.investment_plan.stance == "bullish"  # fixture: trend up + positive news/mood
    assert state.investment_plan.debate_scorecard == DEBATE_DISABLED_SCORECARD
    assert state.investment_plan.cost_usd == 0
    # Everything downstream still runs: trader -> risk debate -> PM -> gate -> execute -> journal.
    assert state.trade_proposal is not None and state.trade_proposal.action == "BUY"
    assert state.risk_transcript, "risk debate must be unchanged by the research-debate switch"
    assert state.pm_decision is not None and state.pm_decision.verdict == "APPROVE"
    assert state.final_order is not None
    assert active.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert active.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 1
    rollup_rows = active.conn.execute(
        "SELECT agent FROM reports WHERE run_id = ? AND agent = ?", (state.run_id, ROLLUP_AGENT_NAME)
    ).fetchall()
    assert len(rollup_rows) == 1


async def test_debate_on_still_runs_bull_bear_and_research_manager(tmp_path) -> None:
    llm = fake_llm()
    active = runner(tmp_path, llm=llm, debate_enabled=True)

    state = await active.run("NVDA", depth="standard")

    assert state.status == "completed"
    assert state.debate_enabled is True
    assert len(state.debate_transcript) == 2  # 1 round: bull + bear
    assert {"bull_researcher", "bear_researcher", "research_manager"} <= {c.agent for c in llm.calls}
    assert state.investment_plan is not None and state.investment_plan.agent == "research_manager"


async def test_run_record_distinguishes_debate_on_and_off(tmp_path) -> None:
    on = runner(tmp_path / "on", llm=fake_llm(), debate_enabled=True)
    off = runner(tmp_path / "off", llm=_llm_without_debate_roles(), debate_enabled=False)

    on_state = await on.run("NVDA")
    off_state = await off.run("NVDA")

    assert get_run(on.conn, on_state.run_id).debate_enabled is True
    assert get_run(off.conn, off_state.run_id).debate_enabled is False
    # The same query `sentinel history --debate off` issues:
    off_rows = off.conn.execute("SELECT run_id FROM runs WHERE debate_enabled = 0").fetchall()
    assert [row["run_id"] for row in off_rows] == [off_state.run_id]
    assert off.conn.execute("SELECT COUNT(*) FROM runs WHERE debate_enabled = 1").fetchone()[0] == 0


async def test_debate_off_survives_checkpoint_resume(tmp_path) -> None:
    llm = _llm_without_debate_roles()
    active = runner(tmp_path, llm=llm, debate_enabled=False)

    paused = await active.run("NVDA", stop_after="research_manager")
    resumed = await active.resume(paused.run_id)

    assert paused.investment_plan is not None and paused.investment_plan.agent == ROLLUP_AGENT_NAME
    assert resumed.status == "completed"
    assert resumed.debate_enabled is False
    assert resumed.final_order is not None

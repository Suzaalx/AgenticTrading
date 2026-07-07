from __future__ import annotations

from ._helpers import fake_llm, runner


async def test_hold_short_circuits_risk_debate_and_order(tmp_path) -> None:
    llm = fake_llm(action="HOLD")
    active = runner(tmp_path, llm=llm)

    state = await active.run("NVDA")

    assert state.status == "completed"
    assert state.trade_proposal is not None
    assert state.trade_proposal.action == "HOLD"
    assert state.risk_transcript == []
    assert state.final_order is None
    assert "aggressive_risk" not in [call.agent for call in llm.calls]
    assert active.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

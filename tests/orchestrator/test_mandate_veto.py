from __future__ import annotations

from ._helpers import fake_llm, mandate, runner


async def test_mandate_gate_vetoes_after_pm_approval(tmp_path) -> None:
    active = runner(tmp_path, llm=fake_llm(), mandate_value=mandate(universe=["AAPL"]))

    state = await active.run("NVDA")

    assert state.status == "completed"
    assert state.pm_decision is not None
    assert state.pm_decision.verdict == "APPROVE"
    assert state.final_order is None
    assert state.error is not None
    assert "SYMBOL_NOT_IN_UNIVERSE" in state.error
    assert active.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

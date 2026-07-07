from __future__ import annotations

from sentinel.risk.killswitch import engage

from ._helpers import fake_llm, runner


async def test_kill_switch_halts_before_execute(tmp_path) -> None:
    active = runner(tmp_path, llm=fake_llm())
    state = await active.run("NVDA", stop_after="mandate_gate")
    assert state.final_order is not None

    engage(actor="test")
    resumed = await active.resume(state.run_id)

    assert resumed.status == "halted"
    assert active.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

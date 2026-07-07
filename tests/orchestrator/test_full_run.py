from __future__ import annotations

from sentinel.core.events import DecisionMade, GateEvaluated, OrderFilled, RunStarted, StageChanged

from ._helpers import fake_llm, runner


async def test_full_run_completes_persists_rows_and_emits_ordered_events(tmp_path) -> None:
    llm = fake_llm()
    active = runner(tmp_path, llm=llm)
    queue = await active.bus.subscribe_queue()

    state = await active.run("NVDA", depth="standard")

    assert state.status == "completed"
    assert state.trade_proposal is not None
    assert state.trade_proposal.action == "BUY"
    assert state.pm_decision is not None
    assert state.pm_decision.verdict == "APPROVE"
    assert state.final_order is not None
    assert active.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert active.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] >= 7
    assert active.conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'filled'").fetchone()[0] == 1
    assert active.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert active.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    assert active.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 1

    events = []
    while not queue.empty():
        events.append(await queue.get())
    interesting = [
        type(event)
        for event in events
        if isinstance(event, RunStarted | StageChanged | DecisionMade | GateEvaluated | OrderFilled)
    ]
    assert interesting[0] is RunStarted
    assert StageChanged in interesting
    assert interesting.index(DecisionMade) < interesting.index(GateEvaluated)
    assert interesting.index(GateEvaluated) < interesting.index(OrderFilled)

from __future__ import annotations

from sentinel.orchestrator.runner import OrchestratorRunner

from ._helpers import FixtureRouter, fake_llm, mandate, settings


async def test_resume_continues_from_checkpoint(tmp_path) -> None:
    llm = fake_llm()
    first = OrchestratorRunner(
        settings=settings(),
        mandate=mandate(),
        llm=llm,
        router=FixtureRouter(),  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )
    partial = await first.run("NVDA", stop_after="research_manager")
    assert partial.investment_plan is not None
    assert partial.trade_proposal is None

    second = OrchestratorRunner(
        settings=settings(),
        mandate=mandate(),
        llm=llm,
        router=FixtureRouter(),  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )
    resumed = await second.resume(partial.run_id)

    assert resumed.status == "completed"
    assert resumed.trade_proposal is not None
    assert resumed.final_order is not None

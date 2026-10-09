from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from sentinel.config.settings import LLMSettings, Settings
from sentinel.llm.contracts import LLMResult
from sentinel.llm.cost import record_cost
from sentinel.llm.gateway import LLMGateway
from sentinel.store.repos.costs_repo import list_costs
from sentinel.store.repos.runs_repo import get_run
from sentinel.testing.fakes import FakeLLM

from ._helpers import EventBus, FixtureRouter, OrchestratorRunner, conn, fake_llm, mandate, settings

DEBATERS = {"bull_researcher", "bear_researcher", "debate_convergence_classifier"}


def _runner(tmp_path, llm: FakeLLM, *, planner: str) -> OrchestratorRunner:
    base = settings(debate_enabled=False)
    tuned = base.model_copy(update={"pipeline": base.pipeline.model_copy(update={"debate_off_planner": planner})})
    return OrchestratorRunner(
        bus=EventBus(), conn=conn(tmp_path), settings=tuned, mandate=mandate(), llm=llm,
        router=FixtureRouter(),  # type: ignore[arg-type]
    )


def _llm_without_debaters() -> FakeLLM:
    llm = fake_llm()
    for name in DEBATERS:
        llm.responses.pop(name)
    return llm


async def test_judge_only_variant_keeps_the_research_manager_but_skips_the_debate(tmp_path) -> None:
    llm = _llm_without_debaters()  # any debater call would raise KeyError
    active = _runner(tmp_path, llm, planner="research_manager")

    state = await active.run("NVDA", depth="standard")

    assert state.status == "completed"
    assert state.debate_transcript == []
    assert not DEBATERS & {c.agent for c in llm.calls}
    rm_calls = [c for c in llm.calls if c.agent == "research_manager"]
    assert len(rm_calls) == 1
    assert "NO bull/bear debate" in rm_calls[0].prompt and "TRANSCRIPT" not in rm_calls[0].prompt
    assert state.investment_plan is not None and state.investment_plan.agent == "research_manager"
    assert state.trade_proposal is not None and state.final_order is not None  # rest of pipeline unchanged
    record = get_run(active.conn, state.run_id)
    assert record.debate_enabled is False and record.debate_variant == "off_judge"


async def test_rollup_variant_is_unchanged_and_labelled(tmp_path) -> None:
    llm = _llm_without_debaters()
    llm.responses.pop("research_manager")
    active = _runner(tmp_path, llm, planner="rollup")

    state = await active.run("NVDA", depth="standard")

    assert state.investment_plan is not None and state.investment_plan.agent == "analyst_rollup"
    assert get_run(active.conn, state.run_id).debate_variant == "off_rollup"


async def test_debate_on_is_labelled_on(tmp_path) -> None:
    from ._helpers import runner

    active = runner(tmp_path, llm=fake_llm())
    state = await active.run("NVDA")

    assert get_run(active.conn, state.run_id).debate_variant == "on"
    assert "TRANSCRIPT" in next(c for c in active.llm.calls if c.agent == "research_manager").prompt  # type: ignore[attr-defined]


class _Payload(BaseModel):
    value: int


class _FlakyProvider:
    """First answer fails validation, the retry succeeds - like the open-model failures in the pilot."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> LLMResult[Any]:
        self.calls += 1
        payload = {"value": "not-a-number"} if self.calls == 1 else {"value": 7}
        return LLMResult(content=str(payload), structured=payload, input_tokens=100, output_tokens=10,
                         model="m", latency_ms=50)


async def test_gateway_reports_attempts_and_meters_both_attempts(tmp_path) -> None:
    gateway = LLMGateway(settings=Settings(llm=LLMSettings(provider="groq", max_retries=0)), provider=_FlakyProvider())

    result = await gateway.complete_structured("trader", "p", _Payload)

    assert result.structured.value == 7
    assert result.attempts == 2
    assert (result.input_tokens, result.output_tokens) == (200, 20)  # rejected first answer is counted
    assert result.latency_ms == 100

    db = conn(tmp_path)
    record_cost(db, run_id="r", agent="trader", model="m", tokens_in=200, tokens_out=20, attempts=result.attempts)
    assert list_costs(db, "r")[0].attempts == 2


async def test_first_pass_result_reports_one_attempt() -> None:
    class Good:
        async def complete_structured(self, **_: Any) -> LLMResult[Any]:
            return LLMResult(content="{}", structured={"value": 1}, model="m")

    gateway = LLMGateway(settings=Settings(llm=LLMSettings(provider="groq", max_retries=0)), provider=Good())
    assert (await gateway.complete_structured("a", "p", _Payload)).attempts == 1


@pytest.mark.parametrize("planner", ["rollup", "research_manager"])
def test_setting_round_trips_from_toml(tmp_path, planner) -> None:
    from sentinel.config.settings import load_settings

    (tmp_path / "config.toml").write_text(f'[pipeline]\ndebate_off_planner = "{planner}"\n', encoding="utf-8")
    assert load_settings(tmp_path).pipeline.debate_off_planner == planner

"""Research debate agents and helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from string import Formatter
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, Field

from sentinel.agents.base import Agent, AgentPayload
from sentinel.config.settings import Settings, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentStarted, CostIncurred, DebateTurnAdded
from sentinel.core.models import DebateTurn, InvestmentPlan, Lesson, RunState
from sentinel.llm.budget import assert_within_budget
from sentinel.llm.contracts import LLMResult, StructuredLLM
from sentinel.llm.cost import cost_usd, record_cost
from sentinel.store.db import connect, run_migrations

_MAX_TURN_WORDS = 350
T = TypeVar("T", bound=BaseModel)


class DebatePayload(BaseModel):
    argument: str


class ConvergencePayload(BaseModel):
    answer: Literal["yes", "no"]


class _SafeMapping(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ResearchDebater:
    """Base for bull/bear researchers that produce debate turns."""

    agent_name: ClassVar[str]
    speaker: ClassVar[Literal["bull", "bear"]]
    stance_label: ClassVar[str]
    prompt_template: ClassVar[str]
    tier: ClassVar[Literal["quick"]] = "quick"

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
        lessons: list[Lesson] | None = None,
    ) -> None:
        self.llm = llm
        self.bus = bus
        self.settings = settings or load_settings()
        self.conn = conn or connect()
        if conn is None:
            run_migrations(self.conn)
        self.lessons = lessons or []

    async def run(
        self,
        state: RunState,
        *,
        round_number: int,
        lessons: list[Lesson] | None = None,
    ) -> DebateTurn:
        """Generate one capped research debate turn."""

        assert_within_budget(self.conn, Decimal(str(self.settings.llm.monthly_budget_usd)))
        model = self._model_for_start()
        await self.bus.publish(AgentStarted(run_id=state.run_id, agent=self.agent_name, model=model))
        prompt = self.build_prompt(state, round_number=round_number, lessons=lessons)
        result = await self.complete_structured_metered(
            run_id=state.run_id,
            agent_name=self.agent_name,
            prompt=prompt,
            schema=DebatePayload,
            tier=self.tier,
        )
        argument = _payload_text(result.structured, fallback=result.content)
        return DebateTurn(
            speaker=self.speaker,
            round=round_number,
            argument=_truncate_words(argument),
        )

    async def complete_structured_metered(
        self,
        *,
        run_id: str,
        agent_name: str,
        prompt: str,
        schema: type[T],
        tier: Literal["quick", "deep"],
    ) -> LLMResult[T | Any]:
        """Call the LLM behind the same budget stop and cost ledger as agent runs."""

        assert_within_budget(self.conn, Decimal(str(self.settings.llm.monthly_budget_usd)))
        result = await self.llm.complete_structured(
            agent=agent_name,
            prompt=prompt,
            schema=schema,
            tier=tier,
        )
        incurred = cost_usd(result.model, result.input_tokens, result.output_tokens)
        record_cost(
            self.conn,
            run_id=run_id,
            agent=agent_name,
            model=result.model,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            cost=incurred,
        )
        await self.bus.publish(
            CostIncurred(
                run_id=run_id,
                agent=agent_name,
                model=result.model,
                tokens_in=result.input_tokens,
                tokens_out=result.output_tokens,
                cost_usd=incurred,
            )
        )
        return result

    def build_prompt(
        self,
        state: RunState,
        *,
        round_number: int,
        lessons: list[Lesson] | None = None,
    ) -> str:
        template_path = Path(__file__).with_name("prompts") / self.prompt_template
        template = template_path.read_text(encoding="utf-8")
        values = _SafeMapping(
            {
                "run_id": state.run_id,
                "symbol": state.symbol,
                "as_of": state.as_of.isoformat(),
                "round_number": str(round_number),
                "analyst_reports": render_agent_reports(state.analyst_reports),
                "transcript": render_transcript(state.debate_transcript),
                "lessons": render_lessons(_combine_lessons(state, self.lessons, lessons)),
            }
        )
        return Formatter().vformat(template, (), values)

    def _model_for_start(self) -> str:
        model_for_tier = getattr(self.llm, "model_for_tier", None)
        if callable(model_for_tier):
            return str(model_for_tier(self.tier))
        return str(getattr(self.llm, "model", "unknown"))


class BullResearcher(ResearchDebater):
    """Bull researcher arguing the strongest long case."""

    agent_name: ClassVar[str] = "bull_researcher"
    speaker: ClassVar[Literal["bull"]] = "bull"
    stance_label: ClassVar[str] = "bull"
    prompt_template: ClassVar[str] = "bull_researcher.md"


class BearResearcher(ResearchDebater):
    """Bear researcher arguing the strongest downside/avoidance case."""

    agent_name: ClassVar[str] = "bear_researcher"
    speaker: ClassVar[Literal["bear"]] = "bear"
    stance_label: ClassVar[str] = "bear"
    prompt_template: ClassVar[str] = "bear_researcher.md"


class ResearchManager(Agent):
    """Deep-tier judge that converts the research debate into an investment plan."""

    class Payload(AgentPayload):
        stance: Literal["bullish", "bearish", "neutral"]
        conviction: int = Field(ge=0, le=100)
        thesis: str
        key_risks: list[str]
        invalidation: str
        debate_scorecard: str

    agent_name: ClassVar[str] = "research_manager"
    tier: ClassVar[Literal["deep"]] = "deep"
    prompt_template: ClassVar[str] = "research_manager.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[InvestmentPlan]] = InvestmentPlan

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
        lessons: list[Lesson] | None = None,
    ) -> None:
        super().__init__(llm=llm, bus=bus, conn=conn, settings=settings)
        self.lessons = lessons or []

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        return {
            "run_id": state.run_id,
            "symbol": state.symbol,
            "as_of": state.as_of.isoformat(),
            "analyst_reports": render_agent_reports(state.analyst_reports),
            "transcript": render_transcript(state.debate_transcript),
            "lessons": render_lessons(_combine_lessons(state, self.lessons, None)),
        }


async def run_research_debate(
    state: RunState,
    bull: BullResearcher,
    bear: BearResearcher,
    *,
    max_rounds: int | None = None,
    lessons: list[Lesson] | None = None,
) -> list[DebateTurn]:
    """Run bull-first alternating research debate, append turns, and emit turn events."""

    rounds = max_rounds if max_rounds is not None else bull.settings.pipeline.max_debate_rounds
    for round_number in range(1, max(0, rounds) + 1):
        for agent in (bull, bear):
            turn = await agent.run(state, round_number=round_number, lessons=lessons)
            state.debate_transcript.append(turn)
            await agent.bus.publish(
                DebateTurnAdded(run_id=state.run_id, turn=turn, debate="research")
            )
        if not await _last_round_added_material_arguments(state, bull):
            break
    return state.debate_transcript


async def _last_round_added_material_arguments(
    state: RunState,
    agent: ResearchDebater,
) -> bool:
    """Return True when the quick classifier says the latest round added new arguments."""

    latest_round_number = state.debate_transcript[-1].round if state.debate_transcript else 0
    prompt = (
        "Use only the research debate transcript below. Answer yes or no: did the last "
        f"full round (round {latest_round_number}) introduce materially new arguments "
        "rather than repeating prior points?\n\n"
        f"{render_transcript(state.debate_transcript)}"
    )
    try:
        result = await agent.complete_structured_metered(
            run_id=state.run_id,
            agent_name="debate_convergence_classifier",
            prompt=prompt,
            schema=ConvergencePayload,
            tier="quick",
        )
    except KeyError:
        return True
    structured = result.structured
    if isinstance(structured, ConvergencePayload):
        return structured.answer == "yes"
    if isinstance(structured, dict):
        return str(structured.get("answer", "yes")).lower() != "no"
    return "no" not in str(result.content).lower()


def render_agent_reports(reports: Mapping[str, Any]) -> str:
    if not reports:
        return "_No analyst reports provided._"
    blocks: list[str] = []
    for name, report in reports.items():
        dump = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
        blocks.append(f"#### {name}\n```json\n{dump}\n```")
    return "\n\n".join(blocks)


def render_transcript(transcript: list[DebateTurn]) -> str:
    if not transcript:
        return "_Transcript is empty._"
    return "\n".join(
        f"- Round {turn.round} {turn.speaker}: {turn.argument}" for turn in transcript
    )


def render_lessons(lessons: list[Lesson]) -> str:
    header = "### Lessons from your past trades"
    if not lessons:
        return f"{header}\n_No memory lessons provided._"
    rows = [header, "", "| id | grade | tags | lesson | outcome |", "| --- | --- | --- | --- | --- |"]
    for lesson in lessons[:5]:
        rows.append(
            "| "
            + " | ".join(
                [
                    _escape(lesson.lesson_id),
                    lesson.grade,
                    _escape(", ".join(lesson.setup_tags)),
                    _escape(lesson.lesson),
                    _escape(lesson.what_happened),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _payload_text(payload: Any, *, fallback: str) -> str:
    if isinstance(payload, DebatePayload):
        return payload.argument
    if isinstance(payload, dict):
        return str(payload.get("argument") or payload.get("content") or fallback)
    return str(getattr(payload, "argument", fallback))


def _combine_lessons(
    state: RunState,
    instance_lessons: list[Lesson],
    call_lessons: list[Lesson] | None,
) -> list[Lesson]:
    return [*_state_lessons(state), *instance_lessons, *(call_lessons or [])]


def _state_lessons(state: RunState) -> list[Lesson]:
    raw = getattr(state, "lessons", None)
    if raw is None:
        template_values = getattr(state, "template_values", None)
        if isinstance(template_values, Mapping):
            raw = template_values.get("lessons")
    if raw is None:
        return []
    return [item if isinstance(item, Lesson) else Lesson.model_validate(item) for item in raw]


def _truncate_words(text: str, *, max_words: int = _MAX_TURN_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " …"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")

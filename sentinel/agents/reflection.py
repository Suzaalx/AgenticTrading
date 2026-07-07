"""Deep-tier reflection agent for turning outcomes into reusable lessons."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import Field

from sentinel.agents.base import Agent, AgentPayload
from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.models import AgentReport, JournalEntry, Lesson, RunState
from sentinel.llm.contracts import StructuredLLM

LessonGrade = Literal["good_call", "bad_call", "lucky", "unlucky"]


class ReflectionReport(AgentReport):
    """Agent envelope plus reflection payload."""

    setup_tags: list[str]
    what_happened: str
    lesson: str
    grade: LessonGrade


class ReflectionAgent(Agent):
    """Reflect on a completed trade outcome and emit a reusable lesson."""

    class Payload(AgentPayload):
        setup_tags: list[str] = Field(default_factory=list)
        what_happened: str
        lesson: str
        grade: LessonGrade

    agent_name: ClassVar[str] = "reflection"
    tier: ClassVar[Literal["deep"]] = "deep"
    prompt_template: ClassVar[str] = "reflection.md"
    response_schema: ClassVar[type[Payload]] = Payload
    report_type: ClassVar[type[ReflectionReport]] = ReflectionReport

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
        journal_entry: JournalEntry | None = None,
        run_summary: str | None = None,
        realized_ret: float | None = None,
        bench_ret: float | None = None,
        benchmark_symbol: str = "SPY",
    ) -> None:
        super().__init__(llm=llm, bus=bus, conn=conn, settings=settings)
        self.journal_entry = journal_entry
        self.run_summary = run_summary
        self.realized_ret = realized_ret
        self.bench_ret = bench_ret
        self.benchmark_symbol = benchmark_symbol

    def build_template_values(self, state: RunState) -> Mapping[str, Any]:
        entry = self.journal_entry
        option_ror = _option_return_on_risk(entry)
        return {
            "run_id": state.run_id,
            "symbol": entry.symbol if entry else state.symbol,
            "as_of": state.as_of.isoformat(),
            "decision_date": entry.date.isoformat() if entry else state.as_of.date().isoformat(),
            "stance": entry.stance if entry else _value_or_unknown(state.investment_plan, "stance"),
            "action": entry.action if entry else _value_or_unknown(state.trade_proposal, "action"),
            "conviction": entry.conviction if entry else _value_or_unknown(state.investment_plan, "conviction"),
            "size": entry.size if entry else _value_or_unknown(state.trade_proposal, "quantity_pct"),
            "thesis_summary": entry.thesis_summary if entry else _value_or_unknown(state.investment_plan, "thesis"),
            "invalidation": entry.invalidation if entry else _value_or_unknown(state.investment_plan, "invalidation"),
            "horizon_end": entry.horizon_end.isoformat() if entry and entry.horizon_end else "unknown",
            "realized_ret": _format_return(self.realized_ret),
            "bench_ret": _format_return(self.bench_ret),
            "strategy": entry.strategy if entry and entry.strategy else "none",
            "venue": entry.venue if entry else "paper",
            "option_max_loss": entry.size if entry is not None and _is_option_entry(entry) else "not_applicable",
            "option_return_on_risk": _format_return(option_ror),
            "benchmark_symbol": self.benchmark_symbol,
            "run_summary": self.run_summary or _summarize_state(state),
        }

    async def run(self, state: RunState) -> ReflectionReport:
        """Reflect, then deterministically tag and grade option outcomes."""

        report = await super().run(state)
        if isinstance(report, ReflectionReport) and _is_option_entry(self.journal_entry):
            report.setup_tags = _option_setup_tags(self.journal_entry, state, report.setup_tags)
            report.grade = _grade_option_entry(self.journal_entry, self.bench_ret)
        return report  # type: ignore[return-value]


def lesson_from_report(report: ReflectionReport, *, lesson_id: str, symbol: str) -> Lesson:
    """Convert a reflected agent report into the public Lesson model."""

    return Lesson(
        lesson_id=lesson_id,
        created_at=report.created_at,
        symbol=symbol.upper(),
        setup_tags=report.setup_tags,
        what_happened=report.what_happened,
        lesson=report.lesson,
        grade=report.grade,
    )


def _format_return(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.4%}"


def _is_option_entry(entry: JournalEntry | None) -> bool:
    return entry is not None and entry.action in {"OPEN_OPTION", "CLOSE_OPTION"}


def _option_return_on_risk(entry: JournalEntry | None) -> float | None:
    if not _is_option_entry(entry) or entry is None or entry.realized_ret is None or entry.size <= 0:
        return None
    return entry.realized_ret / entry.size


def _grade_option_entry(entry: JournalEntry | None, bench_ret: float | None) -> LessonGrade:
    ror = _option_return_on_risk(entry)
    if ror is None:
        return "bad_call"
    benchmark = 0.0 if bench_ret is None else bench_ret
    if ror >= benchmark and ror >= 0:
        return "good_call"
    if ror >= 0:
        return "unlucky"
    if benchmark < 0 and ror > benchmark:
        return "lucky"
    return "bad_call"


def _option_setup_tags(entry: JournalEntry | None, state: RunState, existing: list[str]) -> list[str]:
    tags = list(dict.fromkeys(existing))
    iv_rank = state.option_chain.iv_rank if state.option_chain is not None else None
    iv_label = "mid" if iv_rank is None else "high" if iv_rank >= 0.66 else "low" if iv_rank <= 0.33 else "mid"
    additions = [f"iv_rank:{iv_label}"]
    if entry is not None and entry.strategy is not None:
        additions.append(f"strategy:{entry.strategy}")
        additions.append(f"venue:{'paper' if entry.venue == 'paper' else 'live'}")
    for tag in additions:
        if tag not in tags:
            tags.append(tag)
    return tags


def _value_or_unknown(obj: object, attr: str) -> object:
    return getattr(obj, attr, "unknown") if obj is not None else "unknown"


def _summarize_state(state: RunState) -> str:
    lines = [
        f"Run {state.run_id} for {state.symbol} completed with status {state.status}.",
        f"Analyst reports: {', '.join(state.analyst_reports) or 'none'}.",
    ]
    if state.investment_plan is not None:
        lines.append(f"Investment plan: {state.investment_plan.content}")
    if state.trade_proposal is not None:
        lines.append(f"Trade proposal: {state.trade_proposal.content}")
    if state.option_proposal is not None:
        lines.append(f"Option proposal: {state.option_proposal.content}")
    if state.pm_decision is not None:
        lines.append(f"PM decision: {state.pm_decision.content}")
    if state.error:
        lines.append(f"Run error: {state.error}")
    return "\n".join(lines)


def reflection_state_for_entry(entry: JournalEntry) -> RunState:
    """Build the minimal RunState needed by ReflectionAgent for a journal row."""

    as_of = datetime.combine(entry.date, datetime.min.time(), tzinfo=UTC)
    return RunState(
        run_id=entry.run_id,
        symbol=entry.symbol,
        as_of=as_of,
        mode="decision",
        status="completed",
        snapshot=None,
        investment_plan=None,
        trade_proposal=None,
        pm_decision=None,
        final_order=None,
        error=None,
    )

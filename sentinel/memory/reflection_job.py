"""Daily/on-demand reflection job for matured journal entries."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import cast

from sentinel.agents.reflection import (
    ReflectionAgent,
    ReflectionReport,
    lesson_from_report,
    reflection_state_for_entry,
)
from sentinel.core.bus import EventBus
from sentinel.core.events import LogLine
from sentinel.core.models import JournalEntry, Lesson
from sentinel.llm.contracts import StructuredLLM
from sentinel.memory.journal import find_matured_entries, mark_journal_reflected
from sentinel.memory.recall import enforce_hygiene, store_lesson
from sentinel.store.db import connect, run_migrations

type ReturnsFunction = Callable[[str, date, date], float | Awaitable[float]]


def run_reflection_job(
    conn: sqlite3.Connection | None = None,
    *,
    llm: StructuredLLM | None = None,
    returns_fn: ReturnsFunction | None = None,
    bus: EventBus | None = None,
    as_of: date | datetime | None = None,
    limit: int | None = None,
    benchmark_symbol: str = "SPY",
) -> list[Lesson]:
    """Run the reflection job synchronously.

    ``returns_fn`` is called as ``returns_fn(symbol, start_date, end_date)`` and returns the
    total return for that window as a decimal fraction (0.05 means +5%).
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_reflection_job_async(
                conn,
                llm=llm,
                returns_fn=returns_fn,
                bus=bus,
                as_of=as_of,
                limit=limit,
                benchmark_symbol=benchmark_symbol,
            )
        )
    msg = "run_reflection_job cannot be called from a running event loop; use run_reflection_job_async"
    raise RuntimeError(msg)


async def run_reflection_job_async(
    conn: sqlite3.Connection | None = None,
    *,
    llm: StructuredLLM | None = None,
    returns_fn: ReturnsFunction | None = None,
    bus: EventBus | None = None,
    as_of: date | datetime | None = None,
    limit: int | None = None,
    benchmark_symbol: str = "SPY",
) -> list[Lesson]:
    """Reflect all matured, ungraded journal entries and store resulting lessons."""

    owns_conn = conn is None
    active_conn = conn or connect()
    run_migrations(active_conn)
    active_bus = bus or EventBus()
    try:
        scoring_date = _as_date(as_of or datetime.now(UTC))
        entries = find_matured_entries(
            active_conn,
            as_of=scoring_date,
            include_reflected=False,
            limit=limit,
        )
        if not entries:
            return []
        active_llm = llm or _default_llm()
        active_returns_fn = returns_fn or local_close_return
        lessons: list[Lesson] = []
        for entry in entries:
            lesson = await _reflect_entry(
                active_conn,
                entry,
                llm=active_llm,
                returns_fn=active_returns_fn,
                bus=active_bus,
                scoring_date=scoring_date,
                benchmark_symbol=benchmark_symbol,
            )
            lessons.append(lesson)
        if lessons:
            archived = enforce_hygiene(active_conn)
            await active_bus.publish(
                LogLine(
                    level="info",
                    message="reflection job completed",
                    context={"reflected": len(lessons), "archived": archived},
                )
            )
        return lessons
    finally:
        if owns_conn:
            active_conn.close()


async def _reflect_entry(
    conn: sqlite3.Connection,
    entry: JournalEntry,
    *,
    llm: StructuredLLM,
    returns_fn: ReturnsFunction,
    bus: EventBus,
    scoring_date: date,
    benchmark_symbol: str,
) -> Lesson:
    outcome_end = entry.horizon_end or scoring_date
    realized_ret = await _maybe_await(returns_fn(entry.symbol, entry.date, outcome_end))
    bench_ret = await _maybe_await(returns_fn(benchmark_symbol, entry.date, outcome_end))
    run_summary = _run_summary(conn, entry, realized_ret=realized_ret, bench_ret=bench_ret)
    agent = ReflectionAgent(
        llm=llm,
        bus=bus,
        conn=conn,
        journal_entry=entry,
        run_summary=run_summary,
        realized_ret=realized_ret,
        bench_ret=bench_ret,
        benchmark_symbol=benchmark_symbol,
    )
    report = cast(ReflectionReport, await agent.run(reflection_state_for_entry(entry)))
    lesson = lesson_from_report(report, lesson_id=f"{entry.run_id}:reflection", symbol=entry.symbol)
    store_lesson(conn, lesson)
    mark_journal_reflected(
        conn,
        run_id=entry.run_id,
        realized_ret=realized_ret,
        bench_ret=bench_ret,
        grade=lesson.grade,
        reflected_at=lesson.created_at,
    )
    await bus.publish(
        LogLine(
            level="info",
            message="journal entry reflected",
            context={"run_id": entry.run_id, "lesson_id": lesson.lesson_id, "grade": lesson.grade},
        )
    )
    return lesson


async def _maybe_await(value: float | Awaitable[float]) -> float:
    if inspect.isawaitable(value):
        return float(await value)
    return float(value)


def _run_summary(
    conn: sqlite3.Connection,
    entry: JournalEntry,
    *,
    realized_ret: float,
    bench_ret: float,
) -> str:
    report_rows = conn.execute(
        "SELECT agent, content FROM reports WHERE run_id = ? ORDER BY agent ASC",
        (entry.run_id,),
    ).fetchall()
    report_lines = [f"- {row['agent']}: {row['content']}" for row in report_rows]
    reports = "\n".join(report_lines) if report_lines else "- No stored agent reports."
    return (
        f"Journaled {entry.action} decision for {entry.symbol} on {entry.date.isoformat()}.\n"
        f"Stance: {entry.stance}; conviction: {entry.conviction}; size: {entry.size}.\n"
        f"Thesis: {entry.thesis_summary}\n"
        f"Invalidation: {entry.invalidation}\n"
        f"Outcome: realized_return={realized_ret:.4%}; benchmark_return={bench_ret:.4%}.\n"
        f"Stored reports:\n{reports}"
    )


def local_close_return(symbol: str, start: date, end: date) -> float:
    """Compute a deterministic local close-to-close return for CLI fallback use."""

    from sentinel.data.loaders import LocalDataLoader

    frame = LocalDataLoader().get_ohlcv(symbol, start, end, "1d")
    if frame.empty or "close" not in frame.columns or len(frame.index) < 2:
        return 0.0
    first = float(frame["close"].iloc[0])
    last = float(frame["close"].iloc[-1])
    if first == 0:
        return 0.0
    return (last / first) - 1.0


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _default_llm() -> StructuredLLM:
    from sentinel.llm.gateway import LLMGateway

    return LLMGateway()

from __future__ import annotations

from datetime import date

from sentinel.core.models import JournalEntry
from sentinel.memory.journal import get_journal_entry, write_journal_entry
from sentinel.memory.recall import get_lesson
from sentinel.memory.reflection_job import run_reflection_job
from sentinel.store.db import connect, run_migrations
from sentinel.testing.fakes import FakeLLM


def test_matured_decision_gets_graded_and_stores_lesson(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    write_journal_entry(
        conn,
        JournalEntry(
            run_id="run_reflect",
            symbol="NVDA",
            date=date(2026, 6, 1),
            stance="bullish",
            action="BUY",
            conviction=88,
            size=20.0,
            thesis_summary="Bull debate won on accelerating demand.",
            invalidation="Demand narrative fails.",
            horizon_end=date(2026, 6, 10),
        ),
    )
    llm = FakeLLM(
        {
            "reflection": {
                "content": "NVDA outperformed SPY after the bullish call.",
                "setup_tags": ["bull_won_debate", "momentum"],
                "what_happened": "NVDA gained 12.0% while SPY gained 2.0%.",
                "lesson": "Increase size only when the thesis has a crisp invalidation.",
                "grade": "good_call",
            }
        }
    )

    lessons = run_reflection_job(
        conn,
        llm=llm,
        returns_fn=lambda symbol, _start, _end: 0.12 if symbol == "NVDA" else 0.02,
        as_of=date(2026, 6, 30),
    )

    assert len(lessons) == 1
    assert lessons[0].lesson_id == "run_reflect:reflection"
    assert get_lesson(conn, "run_reflect:reflection") == lessons[0]
    reflected = get_journal_entry(conn, "run_reflect")
    assert reflected is not None
    assert reflected.realized_ret == 0.12
    assert reflected.bench_ret == 0.02
    assert reflected.graded == "good_call"
    assert reflected.reflected_at is not None
    assert "realized_return: 12.0000%" in llm.calls[0].prompt


from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.core.models import Lesson
from sentinel.memory.recall import format_lessons_for_prompt, recall_lessons, store_lesson
from sentinel.store.db import connect, run_migrations


def test_recall_lessons_returns_top_symbol_and_tag_matches(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    base = datetime(2026, 6, 1, tzinfo=UTC)
    lessons = [
        Lesson(
            lesson_id="nvda_high_rsi",
            created_at=base,
            symbol="NVDA",
            setup_tags=["high_rsi", "earnings_runup"],
            what_happened="The entry chased an exhausted move.",
            lesson="Wait for a pullback when RSI is extended.",
            grade="bad_call",
        ),
        Lesson(
            lesson_id="nvda_momentum",
            created_at=base + timedelta(days=1),
            symbol="NVDA",
            setup_tags=["momentum"],
            what_happened="Momentum continued for three sessions.",
            lesson="Let winners run while invalidation remains intact.",
            grade="good_call",
        ),
        Lesson(
            lesson_id="msft_high_rsi",
            created_at=base + timedelta(days=2),
            symbol="MSFT",
            setup_tags=["high_rsi"],
            what_happened="MSFT faded after a crowded run.",
            lesson="Avoid crowded high-RSI setups without a fresh catalyst.",
            grade="bad_call",
        ),
    ]
    for lesson in lessons:
        store_lesson(conn, lesson)

    recalled = recall_lessons(conn, "NVDA", ["high_rsi"], limit=2)

    assert [lesson.lesson_id for lesson in recalled] == ["nvda_high_rsi", "nvda_momentum"]
    block = format_lessons_for_prompt(recalled)
    assert block.startswith("### Lessons from your past trades")
    assert "Wait for a pullback when RSI is extended." in block


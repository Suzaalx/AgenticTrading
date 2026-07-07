from __future__ import annotations

from datetime import UTC, datetime

from sentinel.core.models import Lesson
from sentinel.memory.recall import format_lessons_for_prompt


def test_format_lessons_for_prompt_contains_injectable_lesson_text() -> None:
    lesson = Lesson(
        lesson_id="lesson_prompt",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        symbol="NVDA",
        setup_tags=["earnings_runup", "high_rsi"],
        what_happened="NVDA reversed after an overextended pre-earnings rally.",
        lesson="Demand a fresh catalyst before approving high-RSI earnings runups.",
        grade="bad_call",
    )

    block = format_lessons_for_prompt([lesson])

    assert block == (
        "### Lessons from your past trades\n"
        "- [bad_call] NVDA (earnings_runup, high_rsi): "
        "Demand a fresh catalyst before approving high-RSI earnings runups. "
        "Outcome: NVDA reversed after an overextended pre-earnings rally."
    )

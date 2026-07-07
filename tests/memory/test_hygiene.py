from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.core.models import Lesson
from sentinel.memory.recall import enforce_hygiene, store_lesson
from sentinel.store.db import connect, run_migrations


def test_hygiene_archives_oldest_never_recalled_lessons_over_cap(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(505):
        store_lesson(
            conn,
            Lesson(
                lesson_id=f"lesson_{index:03d}",
                created_at=base + timedelta(days=index),
                symbol="NVDA",
                setup_tags=["setup"],
                what_happened=f"Outcome {index}",
                lesson=f"Lesson {index}.",
                grade="good_call",
            ),
        )

    archived = enforce_hygiene(conn)

    assert archived == 5
    assert conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 500
    archived_ids = {
        row["lesson_id"]
        for row in conn.execute("SELECT lesson_id FROM lesson_archive ORDER BY lesson_id").fetchall()
    }
    assert archived_ids == {f"lesson_{index:03d}" for index in range(5)}


"""FTS5 lesson recall, prompt formatting, and memory hygiene."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from sentinel.core.models import Lesson

MAX_LESSONS = 500


def ensure_memory_schema(conn: sqlite3.Connection) -> None:
    """Create FTS and auxiliary memory tables used by §12."""

    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS lessons USING fts5(
            lesson_id, symbol, setup_tags, what_happened, lesson, grade, created_at
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lesson_recall_meta (
            lesson_id TEXT PRIMARY KEY,
            recalled_count INT NOT NULL DEFAULT 0,
            last_recalled_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lesson_archive (
            lesson_id TEXT PRIMARY KEY,
            symbol TEXT,
            setup_tags TEXT,
            what_happened TEXT,
            lesson TEXT,
            grade TEXT,
            created_at TEXT,
            recalled_count INT NOT NULL DEFAULT 0,
            archived_at TEXT NOT NULL
        )"""
    )
    conn.commit()


def store_lesson(conn: sqlite3.Connection, lesson: Lesson) -> None:
    """Store or replace a reflection lesson in the FTS5 table."""

    ensure_memory_schema(conn)
    conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (lesson.lesson_id,))
    conn.execute(
        """INSERT INTO lessons
           (lesson_id, symbol, setup_tags, what_happened, lesson, grade, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            lesson.lesson_id,
            lesson.symbol.upper(),
            _serialize_tags(lesson.setup_tags),
            lesson.what_happened,
            lesson.lesson,
            lesson.grade,
            lesson.created_at.isoformat(),
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO lesson_recall_meta (lesson_id, recalled_count) VALUES (?, 0)",
        (lesson.lesson_id,),
    )
    conn.commit()


def get_lesson(conn: sqlite3.Connection, lesson_id: str) -> Lesson | None:
    """Fetch one lesson by id."""

    ensure_memory_schema(conn)
    row = conn.execute("SELECT * FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
    return _row_to_lesson(row) if row is not None else None


def list_lessons(conn: sqlite3.Connection, *, limit: int = 20) -> list[Lesson]:
    """List the newest stored lessons."""

    ensure_memory_schema(conn)
    rows = conn.execute(
        "SELECT * FROM lessons ORDER BY created_at DESC, lesson_id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_lesson(row) for row in rows]


def recall_lessons(
    conn: sqlite3.Connection,
    symbol: str,
    setup_tags: list[str],
    limit: int = 5,
) -> list[Lesson]:
    """Recall top lessons by symbol plus overlapping setup tags using FTS5."""

    ensure_memory_schema(conn)
    normalized_symbol = symbol.upper()
    query = _fts_query([normalized_symbol, *setup_tags])
    if not query:
        return []
    rows = conn.execute(
        """SELECT *, bm25(lessons) AS fts_rank
           FROM lessons
           WHERE lessons MATCH ?
           LIMIT ?""",
        (query, max(limit * 8, limit)),
    ).fetchall()
    scored = [
        (_relevance(row, normalized_symbol, setup_tags), float(row["fts_rank"]), _row_to_lesson(row))
        for row in rows
    ]
    scored.sort(key=lambda item: (-item[0], item[1], -item[2].created_at.timestamp()))
    lessons = [lesson for _, _, lesson in scored[:limit]]
    _mark_recalled(conn, [lesson.lesson_id for lesson in lessons])
    return lessons


def search_lessons(conn: sqlite3.Connection, query: str, *, limit: int = 10) -> list[Lesson]:
    """Search lessons with a user-provided FTS query string."""

    ensure_memory_schema(conn)
    fts_query = _fts_query(_tokenize(query))
    if not fts_query:
        return []
    rows = conn.execute(
        """SELECT *, bm25(lessons) AS fts_rank
           FROM lessons
           WHERE lessons MATCH ?
           ORDER BY fts_rank
           LIMIT ?""",
        (fts_query, limit),
    ).fetchall()
    return [_row_to_lesson(row) for row in rows]


def forget_lesson(conn: sqlite3.Connection, lesson_id: str) -> bool:
    """Delete a lesson and its recall metadata."""

    ensure_memory_schema(conn)
    before = conn.total_changes
    conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (lesson_id,))
    conn.execute("DELETE FROM lesson_recall_meta WHERE lesson_id = ?", (lesson_id,))
    conn.commit()
    return conn.total_changes > before


def format_lessons_for_prompt(lessons: list[Lesson]) -> str:
    """Render the exact lesson block injected into downstream prompts."""

    lines = ["### Lessons from your past trades"]
    if not lessons:
        lines.append("No relevant past lessons.")
        return "\n".join(lines)
    for lesson in lessons:
        tags = ", ".join(lesson.setup_tags) if lesson.setup_tags else "no_tags"
        lines.append(
            f"- [{lesson.grade}] {lesson.symbol} ({tags}): {lesson.lesson} "
            f"Outcome: {lesson.what_happened}"
        )
    return "\n".join(lines)


def enforce_hygiene(conn: sqlite3.Connection, *, max_lessons: int = MAX_LESSONS) -> int:
    """Archive oldest, least-recalled lessons until the active set is within the cap."""

    ensure_memory_schema(conn)
    count = int(conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0])
    overage = count - max_lessons
    if overage <= 0:
        return 0
    rows = conn.execute(
        """SELECT l.*, COALESCE(m.recalled_count, 0) AS recalled_count
           FROM lessons AS l
           LEFT JOIN lesson_recall_meta AS m ON m.lesson_id = l.lesson_id
           ORDER BY COALESCE(m.recalled_count, 0) ASC, l.created_at ASC, l.lesson_id ASC
           LIMIT ?""",
        (overage,),
    ).fetchall()
    archived_at = datetime.now(UTC).isoformat()
    for row in rows:
        conn.execute(
            """INSERT OR REPLACE INTO lesson_archive
               (lesson_id, symbol, setup_tags, what_happened, lesson, grade, created_at,
                recalled_count, archived_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["lesson_id"],
                row["symbol"],
                row["setup_tags"],
                row["what_happened"],
                row["lesson"],
                row["grade"],
                row["created_at"],
                int(row["recalled_count"]),
                archived_at,
            ),
        )
        conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (row["lesson_id"],))
        conn.execute("DELETE FROM lesson_recall_meta WHERE lesson_id = ?", (row["lesson_id"],))
    conn.commit()
    return len(rows)


def _row_to_lesson(row: sqlite3.Row) -> Lesson:
    return Lesson.model_validate(
        {
            "lesson_id": row["lesson_id"],
            "created_at": row["created_at"],
            "symbol": row["symbol"],
            "setup_tags": _deserialize_tags(str(row["setup_tags"] or "")),
            "what_happened": row["what_happened"],
            "lesson": row["lesson"],
            "grade": row["grade"],
        }
    )


def _serialize_tags(tags: list[str]) -> str:
    normalized = [_normalize_tag(tag) for tag in tags]
    return " ".join(tag for tag in normalized if tag)


def _deserialize_tags(value: str) -> list[str]:
    return [tag for tag in value.split() if tag]


def _normalize_tag(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", tag.strip().lower()).strip("_")


def _tokenize(query: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9_]+", query) if token]


def _fts_query(terms: list[str]) -> str:
    normalized = []
    for term in terms:
        cleaned = _normalize_tag(term) if term != term.upper() else re.sub(r"[^A-Za-z0-9_]+", "_", term)
        if cleaned:
            escaped = cleaned.replace('"', '""')
            normalized.append(f'"{escaped}"')
    return " OR ".join(dict.fromkeys(normalized))


def _relevance(row: sqlite3.Row, symbol: str, setup_tags: list[str]) -> int:
    row_symbol = str(row["symbol"]).upper()
    row_tags = set(_deserialize_tags(str(row["setup_tags"] or "")))
    requested = {_normalize_tag(tag) for tag in setup_tags}
    return (10 if row_symbol == symbol else 0) + (2 * len(row_tags & requested))


def _mark_recalled(conn: sqlite3.Connection, lesson_ids: list[str]) -> None:
    if not lesson_ids:
        return
    now = datetime.now(UTC).isoformat()
    for lesson_id in lesson_ids:
        conn.execute(
            """INSERT INTO lesson_recall_meta (lesson_id, recalled_count, last_recalled_at)
               VALUES (?, 1, ?)
               ON CONFLICT(lesson_id) DO UPDATE SET
                   recalled_count = recalled_count + 1,
                   last_recalled_at = excluded.last_recalled_at""",
            (lesson_id, now),
        )
    conn.commit()

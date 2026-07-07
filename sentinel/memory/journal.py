"""Decision journal persistence helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Literal, cast

from sentinel.core.models import JournalEntry

LessonGrade = Literal["good_call", "bad_call", "lucky", "unlucky"]

_JOURNAL_COLUMNS: dict[str, str] = {
    "run_id": "TEXT PRIMARY KEY",
    "symbol": "TEXT",
    "date": "TEXT",
    "stance": "TEXT",
    "action": "TEXT",
    "conviction": "INT",
    "size": "REAL",
    "thesis_summary": "TEXT",
    "invalidation": "TEXT",
    "horizon_end": "TEXT",
    "realized_ret": "REAL",
    "bench_ret": "REAL",
    "graded": "TEXT",
    "reflected_at": "TEXT",
}

_WRITE_COLUMNS = tuple(_JOURNAL_COLUMNS)


def ensure_journal_schema(conn: sqlite3.Connection) -> None:
    """Create or extend the journal table with the §12 fields."""

    conn.execute(
        """CREATE TABLE IF NOT EXISTS journal (
            run_id TEXT PRIMARY KEY,
            symbol TEXT,
            date TEXT,
            stance TEXT,
            action TEXT,
            conviction INT,
            size REAL,
            thesis_summary TEXT,
            invalidation TEXT,
            horizon_end TEXT,
            realized_ret REAL,
            bench_ret REAL,
            graded TEXT,
            reflected_at TEXT
        )"""
    )
    existing = {
        cast(str, row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("PRAGMA table_info(journal)").fetchall()
    }
    for name, definition in _JOURNAL_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE journal ADD COLUMN {name} {definition}")
    conn.commit()


def write_journal_entry(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    """Insert or replace a completed decision-run journal entry."""

    ensure_journal_schema(conn)
    data = _entry_to_db(entry)
    placeholders = ", ".join("?" for _ in _WRITE_COLUMNS)
    columns = ", ".join(_WRITE_COLUMNS)
    updates = ", ".join(f"{column}=excluded.{column}" for column in _WRITE_COLUMNS if column != "run_id")
    conn.execute(
        f"INSERT INTO journal ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
        [data[column] for column in _WRITE_COLUMNS],
    )
    conn.commit()


def get_journal_entry(conn: sqlite3.Connection, run_id: str) -> JournalEntry | None:
    """Return one journal entry by run id."""

    ensure_journal_schema(conn)
    row = conn.execute("SELECT * FROM journal WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_entry(row) if row is not None else None


def iter_journal_entries(conn: sqlite3.Connection) -> Iterator[JournalEntry]:
    """Yield journal entries in decision-date order."""

    ensure_journal_schema(conn)
    rows = conn.execute("SELECT * FROM journal ORDER BY date ASC, run_id ASC").fetchall()
    for row in rows:
        entry = _row_to_entry(row)
        if entry is not None:
            yield entry


def find_matured_entries(
    conn: sqlite3.Connection,
    *,
    as_of: date | datetime | None = None,
    include_reflected: bool = False,
    limit: int | None = None,
) -> list[JournalEntry]:
    """Find entries whose horizon has elapsed and are ready for reflection."""

    ensure_journal_schema(conn)
    as_of_date = _as_date(as_of or datetime.now(UTC))
    has_positions = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'positions'"
        ).fetchone()
        is not None
    )
    if has_positions:
        where = [
            """(
                (horizon_end IS NOT NULL AND horizon_end <= ?)
                OR (
                    horizon_end IS NULL
                    AND action IN ('BUY', 'SELL')
                    AND NOT EXISTS (
                        SELECT 1 FROM positions
                        WHERE positions.source_run_id = journal.run_id
                    )
                )
            )"""
        ]
    else:
        where = ["horizon_end IS NOT NULL", "horizon_end <= ?"]
    params: list[object] = [as_of_date.isoformat()]
    if not include_reflected:
        where.append("reflected_at IS NULL")
    sql = f"SELECT * FROM journal WHERE {' AND '.join(where)} ORDER BY horizon_end ASC, run_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [entry for row in rows if (entry := _row_to_entry(row)) is not None]


def mark_journal_reflected(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    realized_ret: float,
    bench_ret: float,
    grade: LessonGrade,
    reflected_at: datetime | None = None,
) -> None:
    """Persist outcome fields after a reflection lesson has been stored."""

    ensure_journal_schema(conn)
    reflected = reflected_at or datetime.now(UTC)
    conn.execute(
        """UPDATE journal
           SET realized_ret = ?, bench_ret = ?, graded = ?, reflected_at = ?
           WHERE run_id = ?""",
        (realized_ret, bench_ret, grade, reflected.isoformat(), run_id),
    )
    conn.commit()


def _entry_to_db(entry: JournalEntry) -> dict[str, object | None]:
    return {
        "run_id": entry.run_id,
        "symbol": entry.symbol.upper(),
        "date": entry.date.isoformat(),
        "stance": entry.stance,
        "action": entry.action,
        "conviction": entry.conviction,
        "size": entry.size,
        "thesis_summary": entry.thesis_summary,
        "invalidation": entry.invalidation,
        "horizon_end": entry.horizon_end.isoformat() if entry.horizon_end else None,
        "realized_ret": entry.realized_ret,
        "bench_ret": entry.bench_ret,
        "graded": entry.graded,
        "reflected_at": entry.reflected_at.isoformat() if entry.reflected_at else None,
    }


def _row_to_entry(row: sqlite3.Row | None) -> JournalEntry | None:
    if row is None:
        return None
    data = dict(row)
    if not data.get("symbol") or not data.get("date"):
        return None
    return JournalEntry.model_validate(data)


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value

from __future__ import annotations

from datetime import date

from sentinel.core.models import JournalEntry
from sentinel.memory.journal import find_matured_entries, get_journal_entry, write_journal_entry
from sentinel.store.db import connect, run_migrations


def test_write_query_and_find_matured_entries(tmp_path) -> None:
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)
    ready = JournalEntry(
        run_id="run_ready",
        symbol="NVDA",
        date=date(2026, 6, 1),
        stance="bullish",
        action="BUY",
        conviction=82,
        size=15.0,
        thesis_summary="Momentum remained strong into earnings.",
        invalidation="Break below the 20-day moving average.",
        horizon_end=date(2026, 6, 15),
    )
    pending = ready.model_copy(update={"run_id": "run_pending", "horizon_end": date(2026, 7, 15)})

    write_journal_entry(conn, ready)
    write_journal_entry(conn, pending)

    stored = get_journal_entry(conn, "run_ready")
    assert stored == ready
    matured = find_matured_entries(conn, as_of=date(2026, 6, 30))
    assert [entry.run_id for entry in matured] == ["run_ready"]


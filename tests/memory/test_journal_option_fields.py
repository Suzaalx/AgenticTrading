"""Journal persistence must round-trip the option strategy/venue fields (WS7 follow-up)."""

from __future__ import annotations

from datetime import date

from sentinel.core.models import JournalEntry
from sentinel.memory.journal import get_journal_entry, write_journal_entry
from sentinel.store.db import connect, run_migrations


def _conn():
    conn = connect(":memory:")
    run_migrations(conn)
    return conn


def test_option_journal_entry_round_trips_strategy_and_venue() -> None:
    conn = _conn()
    entry = JournalEntry(
        run_id="run-opt-1",
        symbol="AAPL",
        date=date(2026, 7, 6),
        stance="bullish",
        action="OPEN_OPTION",
        conviction=70,
        size=25.0,
        thesis_summary="bull call spread on strength",
        invalidation="close below support",
        strategy="bull_call_spread",
        venue="paper",
    )
    write_journal_entry(conn, entry)

    loaded = get_journal_entry(conn, "run-opt-1")
    assert loaded is not None
    assert loaded.action == "OPEN_OPTION"
    assert loaded.strategy == "bull_call_spread"
    assert loaded.venue == "paper"


def test_equity_journal_entry_defaults_strategy_none_venue_paper() -> None:
    conn = _conn()
    entry = JournalEntry(
        run_id="run-eq-1",
        symbol="NVDA",
        date=date(2026, 7, 6),
        stance="bullish",
        action="BUY",
        conviction=60,
        size=10.0,
        thesis_summary="momentum",
        invalidation="trend break",
    )
    write_journal_entry(conn, entry)

    loaded = get_journal_entry(conn, "run-eq-1")
    assert loaded is not None
    assert loaded.strategy is None
    assert loaded.venue == "paper"

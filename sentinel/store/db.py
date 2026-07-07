"""SQLite connection helpers and Sentinel persistence root."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from sentinel.store.migrations import DDL_STATEMENTS


def sentinel_home() -> Path:
    """Return the Sentinel home, honoring SENTINEL_HOME, and ensure subdirs exist."""

    home = Path(os.environ.get("SENTINEL_HOME", str(Path.home() / ".sentinel"))).expanduser()
    (home / "runs").mkdir(parents=True, exist_ok=True)
    (home / "cache").mkdir(parents=True, exist_ok=True)
    return home


def default_db_path() -> Path:
    """Return the default SQLite database path."""

    return sentinel_home() / "sentinel.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection and enable WAL mode."""

    db_path = Path(path) if path is not None else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all idempotent Phase 0 migrations."""

    for statement in DDL_STATEMENTS:
        conn.execute(statement)
    conn.commit()

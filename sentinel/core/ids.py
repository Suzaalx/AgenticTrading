"""ULID helpers for stable Sentinel identifiers."""

from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    """Return a new ULID string."""

    return str(ULID())


def new_run_id() -> str:
    """Return a new run ULID string."""

    return new_id()

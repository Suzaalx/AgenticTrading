"""File-based Sentinel kill switch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sentinel.core.events import KillSwitchChanged
from sentinel.risk._events import emit_event
from sentinel.risk.audit import append_audit
from sentinel.store.db import sentinel_home


def kill_path() -> Path:
    """Return the kill-switch sentinel file path."""

    return sentinel_home() / "KILL"


def is_engaged() -> bool:
    """Return whether the kill switch is currently engaged."""

    return kill_path().exists()


def engage(actor: str = "system") -> None:
    """Engage the kill switch by creating the sentinel file and auditing it."""

    path = kill_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"engaged_by={actor}\nts={datetime.now(UTC).isoformat()}\n", encoding="utf-8")
    append_audit("kill_switch_changed", {"enabled": True}, actor=actor)
    emit_event(KillSwitchChanged(enabled=True, actor=actor))


def disengage(actor: str = "system") -> None:
    """Disengage the kill switch by removing the sentinel file and auditing it."""

    path = kill_path()
    if path.exists():
        path.unlink()
    append_audit("kill_switch_changed", {"enabled": False}, actor=actor)
    emit_event(KillSwitchChanged(enabled=False, actor=actor))

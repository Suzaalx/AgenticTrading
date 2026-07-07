"""Append-only audit ledger for safety-critical Sentinel actions."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

from pydantic import BaseModel

from sentinel.store.db import sentinel_home

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]


class AuditRecord(TypedDict):
    """JSON-compatible audit row persisted to the JSONL ledger."""

    ts: str
    kind: str
    payload: JsonValue
    actor: str


def audit_path() -> Path:
    """Return the audit ledger path."""

    return sentinel_home() / "audit.jsonl"


def _to_json_value(value: object) -> JsonValue:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _to_json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _to_json_value(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence = cast(Sequence[object], value)
        return [_to_json_value(item) for item in sequence]
    return str(value)


def append_audit(
    kind: str,
    payload: Mapping[str, object] | None = None,
    actor: str = "system",
) -> AuditRecord:
    """Append a fsynced audit record and return the JSON-compatible row."""

    record: AuditRecord = {
        "ts": datetime.now(UTC).isoformat(),
        "kind": kind,
        "payload": _to_json_value(dict(payload or {})),
        "actor": actor,
    }
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
        ledger.write("\n")
        ledger.flush()
        os.fsync(ledger.fileno())
    return record


def _parse_record(raw_line: str) -> AuditRecord | None:
    try:
        decoded = cast(object, json.loads(raw_line))
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    row = cast(dict[str, object], decoded)
    ts = row.get("ts")
    kind = row.get("kind")
    payload = row.get("payload")
    actor = row.get("actor")
    if not isinstance(ts, str) or not isinstance(kind, str) or not isinstance(actor, str):
        return None
    return {
        "ts": ts,
        "kind": kind,
        "payload": cast(JsonValue, payload),
        "actor": actor,
    }


def read_audit(limit: int | None = None) -> list[AuditRecord]:
    """Read audit rows, skipping corrupt lines and returning the most recent limit."""

    path = audit_path()
    if not path.exists():
        return []
    records: list[AuditRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = _parse_record(line)
        if record is not None:
            records.append(record)
    if limit is None or limit < 0:
        return records
    return records[-limit:]

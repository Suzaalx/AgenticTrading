"""Disk cache for provider data frames and JSON payloads."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.store.db import sentinel_home


class DiskCache:
    """Parquet/JSON disk cache under ``sentinel_home()/cache``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (sentinel_home() / "cache")
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, provider: str, symbol: str, interval: str, start: date, end: date) -> str:
        raw = "|".join(
            [provider.lower(), symbol.upper(), interval, start.isoformat(), end.isoformat()]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_frame(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        *,
        now: datetime | None = None,
    ) -> pd.DataFrame | None:
        key = self.key(provider, symbol, interval, start, end)
        data_path = self.root / f"{key}.parquet"
        meta_path = self.root / f"{key}.json"
        if not data_path.exists() or not self.is_fresh(meta_path, interval, end, now=now):
            return None
        return pd.read_parquet(data_path)

    def set_frame(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        frame: pd.DataFrame,
        *,
        now: datetime | None = None,
    ) -> Path:
        key = self.key(provider, symbol, interval, start, end)
        data_path = self.root / f"{key}.parquet"
        frame.to_parquet(data_path)
        self._write_meta(provider, symbol, interval, start, end, data_path, now=now)
        return data_path

    def get_json(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        key = self.key(provider, symbol, interval, start, end)
        data_path = self.root / f"{key}.payload.json"
        meta_path = self.root / f"{key}.json"
        if not data_path.exists() or not self.is_fresh(meta_path, interval, end, now=now):
            return None
        return json.loads(data_path.read_text(encoding="utf-8"))

    def set_json(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        payload: dict[str, Any] | list[Any],
        *,
        now: datetime | None = None,
    ) -> Path:
        key = self.key(provider, symbol, interval, start, end)
        data_path = self.root / f"{key}.payload.json"
        data_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self._write_meta(provider, symbol, interval, start, end, data_path, now=now)
        return data_path

    def is_fresh(
        self,
        meta_path: Path,
        interval: str,
        end: date,
        *,
        now: datetime | None = None,
    ) -> bool:
        if not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(str(meta["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        ttl = ttl_for(interval, end, now=now)
        return ttl is None or (now or datetime.now(UTC)) - created_at <= ttl

    def _write_meta(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        data_path: Path,
        *,
        now: datetime | None = None,
    ) -> None:
        created_at = now or datetime.now(UTC)
        meta = {
            "provider": provider,
            "symbol": symbol.upper(),
            "interval": interval,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "data_path": str(data_path),
            "created_at": created_at.isoformat(),
        }
        key = self.key(provider, symbol, interval, start, end)
        (self.root / f"{key}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def ttl_for(interval: str, end: date, *, now: datetime | None = None) -> timedelta | None:
    """Return cache TTL for the requested window, or ``None`` for immutable history."""

    current_date = (now or datetime.now(UTC)).date()
    normalized_interval = interval.lower()
    if normalized_interval not in {"1d", "1day", "daily"}:
        return timedelta(minutes=15)
    if end >= current_date:
        return timedelta(minutes=15)
    if end == current_date - timedelta(days=1):
        return timedelta(hours=24)
    return None

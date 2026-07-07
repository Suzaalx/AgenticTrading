from __future__ import annotations

from datetime import UTC, datetime

from sentinel.data.loaders.local import LocalDataLoader
from sentinel.data.router import DataRouter
from sentinel.data.snapshot import build_snapshot


def test_local_synthetic_ohlcv_marks_snapshot_quality_synthetic(tmp_path) -> None:
    router = DataRouter(loaders=[LocalDataLoader(root=tmp_path)])

    snapshot = build_snapshot(router, "NVDA", datetime(2026, 7, 7, 12, tzinfo=UTC), "SYNTH-RUN")

    assert snapshot.providers_used["ohlcv"] == "local_synthetic"
    assert snapshot.data_quality == "synthetic"

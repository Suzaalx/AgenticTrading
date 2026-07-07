"""Build and persist reproducible data snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sentinel.config.settings import Settings, load_settings
from sentinel.core.ids import new_run_id
from sentinel.core.models import DataSnapshot, FundamentalsSnapshot, NewsItem
from sentinel.data.indicators import compute_indicators
from sentinel.data.router import DataRouter
from sentinel.store.db import sentinel_home


def build_snapshot(
    router: DataRouter,
    symbol: str,
    as_of: datetime,
    run_id: str | None = None,
    settings: Settings | None = None,
) -> DataSnapshot:
    """Persist OHLCV, indicators, news, and fundamentals for one decision run."""

    config = settings or load_settings()
    snapshot_run_id = run_id or new_run_id()
    run_dir = sentinel_home() / "runs" / snapshot_run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    start = as_of.date() - timedelta(days=420)
    end = as_of.date()
    ohlcv = router.get_ohlcv(symbol, start, end, "1d")
    indicators = compute_indicators(ohlcv)

    safe_symbol = symbol.upper().replace("/", "-")
    ohlcv_path = run_dir / f"{safe_symbol}_ohlcv.parquet"
    indicators_path = run_dir / f"{safe_symbol}_indicators.parquet"
    news_path = run_dir / f"{safe_symbol}_news.json"
    fundamentals_path = run_dir / f"{safe_symbol}_fundamentals.json"

    ohlcv.to_parquet(ohlcv_path)
    indicators.to_parquet(indicators_path)

    news = router.get_news(symbol, config.data.news_lookback_days, config.data.news_limit)
    fundamentals = router.get_fundamentals(symbol)
    _write_news(news_path, news)
    _write_fundamentals(fundamentals_path, fundamentals)

    return DataSnapshot(
        run_id=snapshot_run_id,
        symbol=symbol.upper(),
        as_of=as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC),
        ohlcv_path=str(ohlcv_path),
        indicators_path=str(indicators_path),
        news=news,
        fundamentals=fundamentals,
        providers_used=dict(router.providers_used),
    )


def snapshot_sidecar_paths(snapshot: DataSnapshot) -> dict[str, Path]:
    """Return the JSON sidecar paths for a saved snapshot."""

    ohlcv_path = Path(snapshot.ohlcv_path)
    prefix = ohlcv_path.name.removesuffix("_ohlcv.parquet")
    return {
        "news": ohlcv_path.with_name(f"{prefix}_news.json"),
        "fundamentals": ohlcv_path.with_name(f"{prefix}_fundamentals.json"),
    }


def _write_news(path: Path, news: list[NewsItem]) -> None:
    payload = [json.loads(item.model_dump_json()) for item in news]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_fundamentals(path: Path, fundamentals: FundamentalsSnapshot | None) -> None:
    payload: dict[str, Any] | None = (
        json.loads(fundamentals.model_dump_json()) if fundamentals is not None else None
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

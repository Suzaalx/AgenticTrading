"""Risk-free and dividend-yield loaders."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.config.settings import OptionsSettings, Settings, load_settings
from sentinel.core.models import FundamentalsSnapshot
from sentinel.data.cache import DiskCache


@dataclass(frozen=True)
class RateResult:
    """Rate value with provider provenance."""

    value: float
    provider: str
    note: str | None = None


def risk_free_rate(
    as_of: datetime | date,
    *,
    settings: Settings | OptionsSettings | None = None,
    cache: DiskCache | None = None,
) -> RateResult:
    """Return the continuous risk-free rate from ^IRX, falling back to settings."""

    options_settings = _options_settings(settings)
    cache_obj = cache or DiskCache()
    as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
    cached = _get_cached_rate(cache_obj.root, as_of_date)
    if cached is not None:
        return cached

    try:
        import yfinance as yf

        history = yf.Ticker("^IRX").history(
            start=(as_of_date - timedelta(days=7)).isoformat(),
            end=(as_of_date + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
            timeout=8,
        )
        y = _latest_close(history) / 100.0
        value = math.log1p(y)
        result = RateResult(value=value, provider="yfinance:^IRX")
    except Exception as exc:
        result = RateResult(
            value=options_settings.default_risk_free_rate,
            provider="settings.default_risk_free_rate",
            note=f"risk-free rate fallback used: {exc}",
        )
    _set_cached_rate(cache_obj.root, as_of_date, result)
    return result


def dividend_yield(
    underlying: str,
    *,
    fundamentals: FundamentalsSnapshot | None = None,
) -> RateResult:
    """Return the continuous dividend yield when fundamentals expose it, else zero."""

    _ = underlying
    for field in ("dividend_yield", "trailing_annual_dividend_yield"):
        value = getattr(fundamentals, field, None) if fundamentals is not None else None
        if value is not None:
            try:
                return RateResult(value=max(float(value), 0.0), provider="fundamentals")
            except (TypeError, ValueError):
                break
    return RateResult(value=0.0, provider="default_zero", note="dividend yield unavailable; used 0.0")


def _options_settings(settings: Settings | OptionsSettings | None) -> OptionsSettings:
    if settings is None:
        return load_settings().options
    if isinstance(settings, OptionsSettings):
        return settings
    return settings.options


def _latest_close(history: pd.DataFrame) -> float:
    if history is None or history.empty or "Close" not in history.columns:
        msg = "^IRX history unavailable"
        raise RuntimeError(msg)
    close = pd.Series(history["Close"], dtype="float64").dropna()
    if close.empty:
        msg = "^IRX close unavailable"
        raise RuntimeError(msg)
    return float(close.iloc[-1])


def _rate_paths(root: Path, as_of: date) -> tuple[Path, Path]:
    cache_dir = root / "rates"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stem = f"yfinance_irx_{as_of.isoformat()}"
    return cache_dir / f"{stem}.payload.json", cache_dir / f"{stem}.json"


def _get_cached_rate(root: Path, as_of: date) -> RateResult | None:
    payload_path, meta_path = _rate_paths(root, as_of)
    if not payload_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(str(meta["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - created_at > timedelta(days=1):
            return None
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        return RateResult(
            value=float(payload["value"]),
            provider=str(payload["provider"]),
            note=payload.get("note"),
        )
    except Exception:
        return None


def _set_cached_rate(root: Path, as_of: date, result: RateResult) -> None:
    payload_path, meta_path = _rate_paths(root, as_of)
    payload: dict[str, Any] = {
        "value": result.value,
        "provider": result.provider,
        "note": result.note,
    }
    payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "provider": result.provider,
                "symbol": "^IRX",
                "interval": "1d",
                "start": as_of.isoformat(),
                "end": as_of.isoformat(),
                "data_path": str(payload_path),
                "created_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

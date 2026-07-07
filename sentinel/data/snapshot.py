"""Build and persist reproducible data snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from sentinel.config.settings import Settings, load_mandate, load_settings
from sentinel.core.ids import new_run_id
from sentinel.core.models import DataSnapshot, FundamentalsSnapshot, NewsItem, OptionChainSnapshot
from sentinel.data.indicators import compute_indicators
from sentinel.data.router import DataRouter
from sentinel.options.iv import iv_percentile as compute_iv_percentile
from sentinel.options.iv import iv_rank as compute_iv_rank
from sentinel.options.iv import yang_zhang
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

    option_chain = _build_option_chain_artifact(
        router,
        symbol,
        as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC),
        snapshot_run_id,
        ohlcv,
        run_dir,
    )
    if option_chain is not None:
        router.providers_used.update(option_chain.providers_used)

    return DataSnapshot(
        run_id=snapshot_run_id,
        symbol=symbol.upper(),
        as_of=as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC),
        ohlcv_path=str(ohlcv_path),
        indicators_path=str(indicators_path),
        news=news,
        fundamentals=fundamentals,
        providers_used=dict(router.providers_used),
        data_quality=_data_quality(router.providers_used),
    )


def append_iv_history(
    conn: Any,
    symbol: str,
    as_of_date: datetime,
    atm_iv: float | None,
    rv_yz: float | None,
) -> None:
    """Append or replace one IV-history row."""

    if atm_iv is None:
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO iv_history (symbol, date, atm_iv, rv_yz)
        VALUES (?, ?, ?, ?)
        """,
        (symbol.upper(), as_of_date.date().isoformat(), atm_iv, rv_yz),
    )


def compute_iv_context(atm_iv: float | None, history: list[float]) -> tuple[float | None, float | None]:
    """Return IV rank and percentile for a current ATM IV and prior history."""

    if atm_iv is None:
        return None, None
    clean_history = [value for value in history if value >= 0.0]
    return compute_iv_rank(atm_iv, clean_history), compute_iv_percentile(atm_iv, clean_history)


def compute_atm_iv(chain: OptionChainSnapshot) -> float | None:
    """Average IV of the nearest-expiry ATM call/put straddle."""

    if not chain.expiries or not chain.quotes:
        return None
    nearest_expiry = min(chain.expiries)
    spot = float(chain.spot)
    expiry_quotes = [quote for quote in chain.quotes if quote.contract.expiry == nearest_expiry]
    if not expiry_quotes:
        return None
    atm_strike = min(
        expiry_quotes,
        key=lambda quote: abs(float(quote.contract.strike) - spot),
    ).contract.strike
    ivs = [
        quote.implied_vol if quote.implied_vol is not None else quote.model_iv
        for quote in expiry_quotes
        if quote.contract.strike == atm_strike
    ]
    clean_ivs = [value for value in ivs if value is not None and value > 0.0]
    if not clean_ivs:
        return None
    return sum(clean_ivs) / len(clean_ivs)


def compute_yang_zhang_from_ohlcv(ohlcv: pd.DataFrame) -> float | None:
    """Return Yang-Zhang realized volatility for a canonical OHLCV frame."""

    required = ["open", "high", "low", "close"]
    if ohlcv.empty or any(column not in ohlcv.columns for column in required):
        return None
    frame = ohlcv[required].apply(pd.to_numeric, errors="coerce").dropna()
    frame = frame[(frame[required] > 0.0).all(axis=1)]
    if len(frame) < 2:
        return None
    return yang_zhang(
        list(frame["open"].astype(float)),
        list(frame["high"].astype(float)),
        list(frame["low"].astype(float)),
        list(frame["close"].astype(float)),
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


def _build_option_chain_artifact(
    router: DataRouter,
    symbol: str,
    as_of: datetime,
    run_id: str,
    ohlcv: pd.DataFrame,
    run_dir: Path,
) -> OptionChainSnapshot | None:
    if not _options_enabled_for_symbol(symbol):
        return None
    chain = router.get_option_chain(symbol, as_of)
    if chain is None:
        return None
    atm_iv = compute_atm_iv(chain)
    iv_rank, iv_percentile = compute_iv_context(atm_iv, [])
    rv_yz = compute_yang_zhang_from_ohlcv(ohlcv)
    chain = chain.model_copy(
        update={
            "run_id": run_id,
            "atm_iv": atm_iv,
            "iv_rank": iv_rank,
            "iv_percentile": iv_percentile,
            "rv_yang_zhang": rv_yz,
        }
    )
    _write_option_chain(run_dir / "chain.parquet", chain)
    return chain


def _options_enabled_for_symbol(symbol: str) -> bool:
    try:
        mandate_options = load_mandate().options
    except Exception:
        return False
    return mandate_options.enabled and symbol.upper() in {
        underlying.upper() for underlying in mandate_options.underlying_universe
    }


def _write_option_chain(path: Path, chain: OptionChainSnapshot) -> None:
    rows: list[dict[str, Any]] = []
    for quote in chain.quotes:
        rows.append(
            {
                "run_id": chain.run_id,
                "underlying": chain.underlying,
                "as_of": chain.as_of.isoformat(),
                "spot": str(chain.spot),
                "risk_free_rate": chain.risk_free_rate,
                "dividend_yield": chain.dividend_yield,
                "contract_symbol": quote.contract.contract_symbol,
                "kind": quote.contract.kind,
                "strike": str(quote.contract.strike),
                "expiry": quote.contract.expiry.isoformat(),
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "last": str(quote.last) if quote.last is not None else None,
                "volume": quote.volume,
                "open_interest": quote.open_interest,
                "implied_vol": quote.implied_vol,
                "model_iv": quote.model_iv,
                "delta": quote.delta,
                "gamma": quote.gamma,
                "vega": quote.vega,
                "theta": quote.theta,
                "source": quote.source,
                "pricing_source": chain.pricing_source,
                "atm_iv": chain.atm_iv,
                "iv_rank": chain.iv_rank,
                "iv_percentile": chain.iv_percentile,
                "rv_yang_zhang": chain.rv_yang_zhang,
            }
        )
    pd.DataFrame(rows).to_parquet(path)


def _data_quality(providers_used: dict[str, str]) -> Literal["live", "delayed", "synthetic"]:
    providers = set(providers_used.values())
    if "local_synthetic" in providers or "synthetic" in providers:
        return "synthetic"
    if "stooq" in providers:
        return "delayed"
    return "live"

from __future__ import annotations

import pandas as pd

from sentinel.backtest.engine import BacktestConfig, run_backtest_async
from sentinel.core.models import DataSnapshot, Signal


async def test_agent_pipeline_receives_point_in_time_snapshots() -> None:
    frame = _frame()
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    async def fake_pipeline(snapshot: DataSnapshot) -> Signal:
        history = pd.read_parquet(snapshot.ohlcv_path)
        max_seen = pd.Timestamp(history.index.max()).normalize()
        as_of = pd.Timestamp(snapshot.as_of).tz_convert(None).normalize()
        seen.append((max_seen, as_of))
        assert max_seen <= as_of
        return Signal(action="BUY" if len(seen) == 1 else "HOLD", size=1.0)

    result = await run_backtest_async(
        BacktestConfig(
            symbol="SPY",
            mode="agent",
            cadence="daily",
            data=frame,
            starting_cash=1000.0,
            commission_usd=0.0,
            slippage_bps=0.0,
            persist=False,
            bootstrap_iterations=0,
        ),
        pipeline=fake_pipeline,
    )

    assert len(seen) == len(frame)
    assert result.equity_curve[1]["position_qty"] > 0


def _frame() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=6)
    closes = [10.0, 11.0, 12.0, 13.0, 12.0, 14.0]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 1.0 for close in closes],
            "low": [close - 1.0 for close in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=dates,
    )

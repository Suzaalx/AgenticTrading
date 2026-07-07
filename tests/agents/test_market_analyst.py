from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from sentinel.agents.analysts import MarketAnalyst
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentCompleted, AgentStarted, CostIncurred
from sentinel.core.models import DataSnapshot, MarketAnalystReport, RunState
from sentinel.store.db import connect, run_migrations
from sentinel.testing.fakes import FakeLLM


@pytest.mark.asyncio
async def test_market_analyst_run_returns_report_emits_events_and_records_cost(tmp_path) -> None:
    base = tmp_path / "data"
    base.mkdir()
    start = datetime(2026, 3, 1, tzinfo=UTC)
    dates = [start + timedelta(days=day) for day in range(95)]
    ohlcv_path = base / "ohlcv.parquet"
    indicators_path = base / "indicators.parquet"
    pd.DataFrame(
        {
            "date": dates,
            "open": [100 + i for i in range(95)],
            "high": [101 + i for i in range(95)],
            "low": [99 + i for i in range(95)],
            "close": [100.5 + i for i in range(95)],
            "volume": [1_000_000 + i for i in range(95)],
        }
    ).to_parquet(ohlcv_path)
    pd.DataFrame(
        {
            "date": dates,
            "sma_20": [100 + i for i in range(95)],
            "rsi_14": [55.0 for _ in range(95)],
            "macd": [1.2 for _ in range(95)],
        }
    ).to_parquet(indicators_path)
    snapshot = DataSnapshot(
        run_id="run_market",
        symbol="NVDA",
        as_of=dates[-1],
        ohlcv_path=str(ohlcv_path),
        indicators_path=str(indicators_path),
        news=[],
        fundamentals=None,
        providers_used={},
    )
    state = RunState(
        run_id="run_market",
        symbol="NVDA",
        as_of=dates[-1],
        mode="decision",
        status="analyzing",
        snapshot=snapshot,
        investment_plan=None,
        trade_proposal=None,
        pm_decision=None,
        final_order=None,
        error=None,
    )
    llm = FakeLLM(
        {
            "market_analyst": {
                "content": "NVDA is trending up; close 194.5 is above SMA 20 value 194.",
                "trend": "up",
                "support": 188.0,
                "resistance": 200.0,
                "signals": ["RSI 14 is 55.0 on 2026-06-03"],
                "confidence": 72,
            }
        },
        model="gpt-5.4-mini",
    )
    bus = EventBus()
    queue = await bus.subscribe_queue()
    conn = connect(tmp_path / "sentinel.db")
    run_migrations(conn)

    report = await MarketAnalyst(llm=llm, bus=bus, conn=conn).run(state)

    assert isinstance(report, MarketAnalystReport)
    assert report.run_id == "run_market"
    assert report.trend == "up"
    assert report.cost_usd == Decimal("0E-8")
    assert "2026-" in llm.calls[0].prompt
    assert "LAST 90 DAILY BARS" in llm.calls[0].prompt
    events = [await queue.get(), await queue.get(), await queue.get()]
    assert isinstance(events[0], AgentStarted)
    assert isinstance(events[1], CostIncurred)
    assert isinstance(events[2], AgentCompleted)
    row = conn.execute("SELECT * FROM costs WHERE run_id = ?", ("run_market",)).fetchone()
    assert row["agent"] == "market_analyst"

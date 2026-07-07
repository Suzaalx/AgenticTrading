from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest
from textual.widgets import DataTable, Static

from sentinel.core.bus import EventBus
from sentinel.core.events import AgentCompleted, RunStarted
from sentinel.core.models import OptionsAnalystReport
from sentinel.store.db import connect, run_migrations, sentinel_home
from sentinel.tui.app import SentinelApp


def _leg(symbol: str, strike: str = "100", kind: str = "call") -> dict[str, object]:
    return {
        "contract": {
            "contract_symbol": symbol,
            "underlying": "NVDA",
            "kind": kind,
            "strike": strike,
            "expiry": "2026-08-21",
            "multiplier": 100,
        },
        "side": "buy",
        "contracts": 1,
        "limit_price": "2.50",
    }


def _seed_chain(run_id: str = "run-opt") -> None:
    run_dir = sentinel_home() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "run_id": run_id,
                "underlying": "NVDA",
                "as_of": "2026-07-07T19:00:00+00:00",
                "spot": "101",
                "contract_symbol": "NVDA260821C00100000",
                "kind": "call",
                "strike": "100",
                "expiry": "2026-08-21",
                "bid": "2.40",
                "ask": "2.60",
                "delta": 0.55,
                "gamma": 0.03,
                "vega": 0.12,
                "theta": -0.04,
                "source": "live_chain",
            }
        ]
    ).to_parquet(run_dir / "chain.parquet")
    (run_dir / "candidates.json").write_text(
        json.dumps(
            [
                {
                    "candidate_id": "1",
                    "strategy": "long_call",
                    "legs": [_leg("NVDA260821C00100000")],
                    "net_premium": "250",
                    "max_loss": "250",
                    "net_delta": 55,
                    "net_vega": 12,
                    "net_theta": -4,
                }
            ]
        ),
        encoding="utf-8",
    )
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO option_chain_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "NVDA",
                "2026-07-07T19:00:00+00:00",
                "101",
                0.04,
                0.0,
                '["2026-08-21"]',
                0.30,
                0.42,
                0.55,
                0.20,
                "live_chain",
                '{"yfinance":"ok"}',
                str(run_dir / "chain.parquet"),
            ),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_option_positions_render_venue_badge_and_net_greeks() -> None:
    _seed_chain()
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO option_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "pos-1",
                "NVDA",
                "long_call",
                json.dumps([_leg("NVDA260821C00100000")]),
                "250",
                "250",
                "0",
                now,
                "2026-08-21",
                30,
                None,
                None,
                "run-opt",
                "robinhood_agentic",
            ),
        )
        conn.commit()

    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        table = app.query_one("#portfolio-option-positions", DataTable)
        rendered = "\n".join(" ".join(str(cell) for cell in table.get_row_at(i)) for i in range(table.row_count))
        assert "LIVE" in rendered
        assert "NET GREEKS" in rendered
        assert "+55.00" in rendered


@pytest.mark.asyncio
async def test_chain_modal_opens_and_shows_provenance() -> None:
    _seed_chain()
    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert "provenance live_chain" in str(app.screen.query_one("#chain-header", Static).content)


@pytest.mark.asyncio
async def test_live_panel_shows_reconciliation_status() -> None:
    with connect() as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO reconciliations VALUES (?,?,?,?)",
            (
                "2026-07-07T19:00:00+00:00",
                "robinhood_agentic",
                1,
                '{"account_cash":"5000","funding_cap_headroom":"2500"}',
            ),
        )
        conn.commit()
    app = SentinelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")
        panel = str(app.query_one("#live-panel", Static).content)
        assert "Reconciliation OK robinhood_agentic" in panel
        assert "RH cash 5000" in panel


@pytest.mark.asyncio
async def test_run_monitor_shows_options_card_and_candidates() -> None:
    _seed_chain("run-live")
    bus = EventBus()
    app = SentinelApp(event_bus=bus)
    now = datetime(2026, 7, 7, tzinfo=UTC)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await bus.publish(RunStarted(run_id="run-live", symbol="NVDA", as_of=now))
        await bus.publish(
            AgentCompleted(
                run_id="run-live",
                report=OptionsAnalystReport(
                    run_id="run-live",
                    agent="options",
                    model="quick",
                    created_at=now,
                    latency_ms=10,
                    input_tokens=10,
                    output_tokens=10,
                    cost_usd=Decimal("0.01"),
                    content="Options are fair.",
                    iv_regime="fair",
                    expected_move_pct=4.2,
                    skew_note="call skew muted",
                    event_risk=["earnings"],
                    confidence=72,
                ),
            )
        )
        for _ in range(50):
            await pilot.pause()
            if "IV fair" in str(app.query_one("#options-card", Static).content):
                break
        assert "IV fair" in str(app.query_one("#options-card", Static).content)
        candidates = app.query_one("#option-candidates", DataTable)
        assert candidates.row_count == 1
        assert candidates.get_row_at(0)[1] == "long_call"

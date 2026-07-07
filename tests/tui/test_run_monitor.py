from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from textual.widgets import Static, TabbedContent

from sentinel.core.bus import EventBus
from sentinel.core.commands import Depth
from sentinel.core.events import (
    AgentCompleted,
    AgentStarted,
    DebateTurnAdded,
    DecisionMade,
    GateEvaluated,
    OrderFilled,
    OrderSubmitted,
    RunStarted,
    StageChanged,
)
from sentinel.core.models import AgentReport, DebateTurn, Fill, GateResult, Order, PMDecision
from sentinel.tui.app import SentinelApp


class FakeCommandService:
    def __init__(self) -> None:
        self.started: list[tuple[str, date | None, Depth]] = []

    async def start_run(self, symbol: str, run_date: date | None, depth: Depth) -> str:
        self.started.append((symbol, run_date, depth))
        return "fake-run-1"

    async def cancel_run(self, run_id: str) -> None:
        return None

    async def toggle_kill(self, on: bool) -> None:
        return None

    async def close_position(self, symbol: str) -> None:
        return None

    async def start_backtest(self, config: dict[str, Any]) -> str:
        return "bt-1"

    async def reflect_now(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_monitor_streams_scripted_events() -> None:
    bus = EventBus()
    app = SentinelApp(event_bus=bus)
    now = datetime.now(UTC)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")

        await bus.publish(RunStarted(run_id="01JRUN", symbol="NVDA", as_of=now))
        await bus.publish(StageChanged(run_id="01JRUN", stage="fetch_data", status="completed"))
        await bus.publish(StageChanged(run_id="01JRUN", stage="analysts", status="running"))
        await bus.publish(AgentStarted(run_id="01JRUN", agent="market", model="quick"))
        await bus.publish(
            AgentCompleted(
                run_id="01JRUN",
                report=AgentReport(
                    run_id="01JRUN",
                    agent="market",
                    model="quick",
                    created_at=now,
                    latency_ms=100,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=Decimal("0.02"),
                    content="## Technical picture\nTrend is up.",
                ),
            )
        )
        await bus.publish(
            DebateTurnAdded(
                run_id="01JRUN",
                turn=DebateTurn(speaker="bull", round=1, argument="Momentum confirms upside."),
            )
        )
        await bus.publish(
            DebateTurnAdded(
                run_id="01JRUN",
                turn=DebateTurn(speaker="bear", round=1, argument="Valuation risk is elevated."),
            )
        )
        await bus.publish(
            DecisionMade(
                run_id="01JRUN",
                decision=PMDecision(
                    run_id="01JRUN",
                    agent="pm",
                    model="deep",
                    created_at=now,
                    latency_ms=200,
                    input_tokens=120,
                    output_tokens=80,
                    cost_usd=Decimal("0.05"),
                    content="Approve BUY.",
                    verdict="APPROVE",
                    approved_quantity_pct=10.0,
                    reasoning="Risk is acceptable.",
                    lessons_applied=["respect stops"],
                ),
            )
        )
        await bus.publish(GateEvaluated(run_id="01JRUN", result=GateResult(passed=True)))
        order = Order(
            order_id="ord-1",
            run_id="01JRUN",
            symbol="NVDA",
            side="buy",
            qty=Decimal("4"),
            type="market",
            reason="agent_decision",
            created_at=now,
        )
        await bus.publish(OrderSubmitted(order=order))
        await bus.publish(
            OrderFilled(
                order_id="ord-1",
                fill=Fill(
                    order_id="ord-1",
                    price=Decimal("175.90"),
                    qty=Decimal("4"),
                    ts=now,
                    slippage_usd=Decimal("0.10"),
                    commission_usd=Decimal("0"),
                ),
            )
        )
        await pilot.pause()

        pipeline = str(app.query_one("#pipeline-rail", Static).content)
        assert "✓ Fetch data" in pipeline
        assert "✓ Execute" in pipeline

        debate = str(app.query_one("#debate-transcript", Static).content)
        assert "Momentum confirms upside" in debate
        assert "Valuation risk is elevated" in debate

        decision = str(app.query_one("#decision-card", Static).content)
        assert "PM verdict: APPROVE" in decision
        assert "Mandate: ✓ ALL_CHECKS" in decision
        assert "Fill ord-1" in decision


@pytest.mark.asyncio
async def test_start_run_from_tui_then_stream_events() -> None:
    bus = EventBus()
    commands = FakeCommandService()
    app = SentinelApp(event_bus=bus, command_service=commands)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("tab", "tab", "tab", "enter")
        await pilot.pause()

        assert commands.started
        assert commands.started[0][0] == "NVDA"
        assert app.query_one("#main-tabs", TabbedContent).active == "run"

        await bus.publish(
            DebateTurnAdded(
                run_id="fake-run-1",
                turn=DebateTurn(speaker="bull", round=1, argument="Pilot stream works."),
            )
        )
        await pilot.pause()
        debate = str(app.query_one("#debate-transcript", Static).content)
        assert "Pilot stream works" in debate

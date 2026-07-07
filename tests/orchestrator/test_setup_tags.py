from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sentinel.core.models import InvestmentPlan, RunState
from sentinel.orchestrator.graph import OrchestratorGraph


def _envelope() -> dict[str, Any]:
    return {
        "run_id": "01JTEST",
        "agent": "research_manager",
        "model": "fake",
        "created_at": datetime.now(UTC),
        "latency_ms": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": Decimal("0"),
        "content": "ok",
    }


def test_setup_tags_include_debate_winner() -> None:
    plan = InvestmentPlan(
        **_envelope(),
        stance="bearish",
        conviction=65,
        thesis="Downside case is stronger.",
        key_risks=["Demand Slowdown"],
        invalidation="Recover support.",
        debate_scorecard="Bear was more decisive.",
        debate_won_by="bear",
    )
    state = RunState(
        run_id="01JTEST",
        symbol="NVDA",
        as_of=datetime.now(UTC),
        mode="decision",
        status="pending",
        snapshot=None,
        investment_plan=plan,
        trade_proposal=None,
        pm_decision=None,
        final_order=None,
        error=None,
    )

    tags = OrchestratorGraph._setup_tags(state)

    assert "bearish" in tags
    assert "debate_won_by:bear" in tags
    assert "bear_won_debate" in tags
    assert "bull_won_debate" not in tags

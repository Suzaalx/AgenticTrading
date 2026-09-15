from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sentinel.agents.analyst_rollup import build_analyst_rollup_plan
from sentinel.core.models import (
    FundamentalsAnalystReport,
    MarketAnalystReport,
    NewsAnalystReport,
    SentimentAnalystReport,
)
from sentinel.orchestrator.state import initial_state

_ENVELOPE = {
    "run_id": "r",
    "model": "fake",
    "created_at": datetime(2026, 7, 7, tzinfo=UTC),
    "latency_ms": 1,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": Decimal("0"),
}


def _state(*, trend: str, sentiment: str, mood: str, valuation: str, red_flags: list[str] | None = None):
    state = initial_state("NVDA")
    state.analyst_reports = {
        "market_analyst": MarketAnalystReport(
            **_ENVELOPE, agent="market_analyst", content="m", trend=trend, support=90.0, resistance=120.0,
            signals=[], confidence=80,
        ),
        "fundamentals_analyst": FundamentalsAnalystReport(
            **_ENVELOPE, agent="fundamentals_analyst", content="f", valuation=valuation, quality_flags=[],
            red_flags=red_flags or [], upcoming_catalysts=["earnings"], confidence=60,
        ),
        "news_analyst": NewsAnalystReport(
            **_ENVELOPE, agent="news_analyst", content="n", sentiment=sentiment, key_events=[],
            macro_context="", confidence=70,
        ),
        "sentiment_analyst": SentimentAnalystReport(
            **_ENVELOPE, agent="sentiment_analyst", content="s", retail_mood=mood, notable_narratives=[],
            confidence=50,
        ),
    }
    return state


def test_rollup_is_bullish_when_signals_lean_up() -> None:
    plan = build_analyst_rollup_plan(_state(trend="strong_up", sentiment="positive", mood="positive", valuation="fair"))

    assert plan.stance == "bullish"
    assert plan.agent == "analyst_rollup" and plan.cost_usd == 0 and plan.input_tokens == 0
    assert plan.conviction > 50
    assert "below support 90.00" in plan.invalidation
    assert plan.key_risks == ["event risk: earnings"]


def test_rollup_is_bearish_and_surfaces_red_flags() -> None:
    plan = build_analyst_rollup_plan(
        _state(trend="down", sentiment="very_negative", mood="negative", valuation="expensive", red_flags=["debt", "margin"])
    )

    assert plan.stance == "bearish"
    assert plan.key_risks[:2] == ["debt", "margin"]
    assert "negative news flow" in plan.key_risks
    assert "above resistance 120.00" in plan.invalidation


def test_rollup_is_neutral_and_low_conviction_when_signals_conflict() -> None:
    plan = build_analyst_rollup_plan(_state(trend="up", sentiment="negative", mood="neutral", valuation="fair"))

    assert plan.stance == "neutral"
    assert plan.conviction <= 25
    assert plan.debate_won_by == "split"


def test_rollup_is_deterministic() -> None:
    a = build_analyst_rollup_plan(_state(trend="up", sentiment="positive", mood="neutral", valuation="cheap"))
    b = build_analyst_rollup_plan(_state(trend="up", sentiment="positive", mood="neutral", valuation="cheap"))

    assert (a.stance, a.conviction, a.thesis, a.key_risks, a.invalidation) == (
        b.stance, b.conviction, b.thesis, b.key_risks, b.invalidation
    )

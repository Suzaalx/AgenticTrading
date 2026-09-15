"""Deterministic analyst roll-up used when the bull/bear debate is disabled.

With ``[pipeline] debate_enabled = false`` the orchestrator skips the research debate
and the Research Manager judge. The Trader still hard-requires an ``InvestmentPlan``,
so this module folds the analyst reports into one without any LLM call. Keeping it
deterministic means the debate-off ablation measures *exactly* the debate's
contribution, not a second prompt.
"""

from __future__ import annotations

import time
from decimal import Decimal
from statistics import fmean
from typing import Literal

from sentinel.core.events import utc_now
from sentinel.core.models import AgentReport, InvestmentPlan, RunState

ROLLUP_AGENT_NAME = "analyst_rollup"
DEBATE_DISABLED_SCORECARD = (
    "Debate disabled (pipeline.debate_enabled=false): plan synthesized deterministically "
    "from analyst reports; no bull/bear rounds or research-manager judgement were run."
)

_TREND_SCORE = {"strong_up": 2.0, "up": 1.0, "sideways": 0.0, "down": -1.0, "strong_down": -2.0}
_TONE_SCORE = {
    "very_positive": 2.0,
    "positive": 1.0,
    "neutral": 0.0,
    "negative": -1.0,
    "very_negative": -2.0,
}
_VALUATION_SCORE = {"cheap": 1.0, "fair": 0.0, "expensive": -1.0, "unknown": 0.0}
# Retail mood is noisier than the other signals; weight it at half.
_WEIGHTS = {"market_analyst": 1.0, "news_analyst": 1.0, "sentiment_analyst": 0.5, "fundamentals_analyst": 1.0}
_STANCE_THRESHOLD = 1.0


def build_analyst_rollup_plan(state: RunState) -> InvestmentPlan:
    """Fold ``state.analyst_reports`` into an ``InvestmentPlan`` without calling an LLM."""

    started = time.perf_counter()
    reports = state.analyst_reports
    score = _score(reports)
    stance = _stance(score)
    confidences = [int(getattr(r, "confidence", 0)) for r in reports.values() if hasattr(r, "confidence")]
    mean_confidence = fmean(confidences) if confidences else 0.0
    # Conviction scales with how far the signals lean; a neutral roll-up stays low-conviction.
    lean = min(1.0, abs(score) / 3.0) if stance != "neutral" else 0.25
    conviction = round(max(0.0, min(100.0, mean_confidence * lean)))

    market = reports.get("market_analyst")
    fundamentals = reports.get("fundamentals_analyst")
    news = reports.get("news_analyst")
    key_risks = _key_risks(fundamentals, news, stance)
    thesis = _thesis(state.symbol, stance, reports)
    invalidation = _invalidation(stance, market)

    return InvestmentPlan(
        run_id=state.run_id,
        agent=ROLLUP_AGENT_NAME,
        model="none",
        created_at=utc_now(),
        latency_ms=int((time.perf_counter() - started) * 1000),
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal("0"),
        content=thesis,
        stance=stance,
        conviction=conviction,
        thesis=thesis,
        key_risks=key_risks,
        invalidation=invalidation,
        debate_scorecard=DEBATE_DISABLED_SCORECARD,
        debate_won_by="split",
    )


def _score(reports: dict[str, AgentReport]) -> float:
    total = 0.0
    market = reports.get("market_analyst")
    if market is not None:
        total += _WEIGHTS["market_analyst"] * _TREND_SCORE.get(str(getattr(market, "trend", "")), 0.0)
    news = reports.get("news_analyst")
    if news is not None:
        total += _WEIGHTS["news_analyst"] * _TONE_SCORE.get(str(getattr(news, "sentiment", "")), 0.0)
    sentiment = reports.get("sentiment_analyst")
    if sentiment is not None:
        total += _WEIGHTS["sentiment_analyst"] * _TONE_SCORE.get(
            str(getattr(sentiment, "retail_mood", "")), 0.0
        )
    fundamentals = reports.get("fundamentals_analyst")
    if fundamentals is not None:
        total += _WEIGHTS["fundamentals_analyst"] * _VALUATION_SCORE.get(
            str(getattr(fundamentals, "valuation", "")), 0.0
        )
    return total


def _stance(score: float) -> Literal["bullish", "bearish", "neutral"]:
    if score >= _STANCE_THRESHOLD:
        return "bullish"
    if score <= -_STANCE_THRESHOLD:
        return "bearish"
    return "neutral"


def _key_risks(fundamentals: AgentReport | None, news: AgentReport | None, stance: str) -> list[str]:
    risks: list[str] = []
    if fundamentals is not None:
        risks.extend(str(flag) for flag in getattr(fundamentals, "red_flags", [])[:3])
        catalysts = getattr(fundamentals, "upcoming_catalysts", [])
        if catalysts:
            risks.append(f"event risk: {catalysts[0]}")
    if news is not None and str(getattr(news, "sentiment", "")).endswith("negative"):
        risks.append("negative news flow")
    if not risks:
        risks.append("no debate stress-test applied" if stance != "neutral" else "mixed analyst signals")
    return risks[:5]


def _thesis(symbol: str, stance: str, reports: dict[str, AgentReport]) -> str:
    parts = [f"{symbol}: analyst roll-up is {stance} (debate disabled)."]
    market = reports.get("market_analyst")
    if market is not None:
        parts.append(f"Trend {getattr(market, 'trend', 'n/a')}.")
    fundamentals = reports.get("fundamentals_analyst")
    if fundamentals is not None:
        parts.append(f"Valuation {getattr(fundamentals, 'valuation', 'n/a')}.")
    news = reports.get("news_analyst")
    if news is not None:
        parts.append(f"News {getattr(news, 'sentiment', 'n/a')}.")
    sentiment = reports.get("sentiment_analyst")
    if sentiment is not None:
        parts.append(f"Retail mood {getattr(sentiment, 'retail_mood', 'n/a')}.")
    return " ".join(parts)


def _invalidation(stance: str, market: AgentReport | None) -> str:
    support = getattr(market, "support", None) if market is not None else None
    resistance = getattr(market, "resistance", None) if market is not None else None
    if stance == "bullish" and support is not None:
        return f"Daily close below support {support:.2f}."
    if stance == "bearish" and resistance is not None:
        return f"Daily close above resistance {resistance:.2f}."
    return "Analyst signals flip direction on the next refresh."

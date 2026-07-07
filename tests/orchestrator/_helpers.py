from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from sentinel.config.settings import Settings, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.models import FundamentalsSnapshot, Mandate, NewsItem, Quote
from sentinel.orchestrator.runner import OrchestratorRunner
from sentinel.store.db import connect, run_migrations
from sentinel.testing.fakes import FakeLLM


class FixtureRouter:
    providers_used: dict[str, str]

    def __init__(self, *, price: Decimal = Decimal("100")) -> None:
        self.price = price
        self.providers_used = {"ohlcv": "fixture", "news": "fixture", "fundamentals": "fixture"}

    def get_ohlcv(self, symbol: str, start: date, end: date, interval: str = "1d") -> pd.DataFrame:
        _ = (symbol, start, end, interval)
        index = pd.bdate_range("2026-01-01", periods=80, name="date")
        closes = [90.0 + (idx * 0.2) for idx in range(len(index))]
        return pd.DataFrame(
            {
                "open": closes,
                "high": [close + 1.0 for close in closes],
                "low": [close - 1.0 for close in closes],
                "close": closes,
                "volume": [1_000_000] * len(index),
            },
            index=index,
        )

    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]:
        _ = (lookback_days, limit)
        return [
            NewsItem(
                title="Fixture demand update",
                summary="Demand is firm in fixture data.",
                source="fixture",
                url="https://example.invalid/news",
                published=datetime(2026, 7, 6, 12, tzinfo=UTC),
                symbols=[symbol],
            )
        ]

    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot:
        return FundamentalsSnapshot(
            symbol=symbol,
            as_of=date(2026, 7, 6),
            market_cap=1_000_000_000.0,
            pe_ttm=20.0,
            forward_pe=18.0,
            eps_ttm=5.0,
            revenue_growth_yoy=0.2,
            profit_margin=0.3,
            debt_to_equity=0.1,
            free_cash_flow=100_000_000.0,
            next_earnings_date=None,
            analyst_target_mean=120.0,
        )

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol.upper(), price=self.price, ts=datetime.now(UTC), source="fixture")


def conn(tmp_path: Path) -> sqlite3.Connection:
    active = connect(tmp_path / "sentinel.db")
    run_migrations(active)
    return active


def settings(*, budget: float = 25.0, always_run_risk: bool = False) -> Settings:
    base = load_settings(Path.cwd())
    return base.model_copy(
        update={
            "llm": base.llm.model_copy(update={"monthly_budget_usd": budget}),
            "pipeline": base.pipeline.model_copy(
                update={
                    "max_debate_rounds": 1,
                    "max_risk_discuss_rounds": 1,
                    "always_run_risk_debate": always_run_risk,
                    "depth_presets": {"fast": 1, "standard": 1, "deep": 2},
                }
            ),
        }
    )


def mandate(*, universe: list[str] | None = None) -> Mandate:
    return Mandate(
        symbol_universe=universe or ["NVDA", "AAPL", "SPY"],
        max_position_pct_equity=10.0,
        max_order_notional_usd=2000.0,
        max_gross_exposure_pct=80.0,
        max_daily_loss_pct=3.0,
        max_orders_per_day=10,
        allow_short=False,
        cooldown_minutes_per_symbol=60,
    )


def fake_llm(*, action: str = "BUY", verdict: str = "APPROVE") -> FakeLLM:
    quantity = 50.0 if action != "HOLD" else 0.0
    return FakeLLM(
        {
            "market_analyst": {
                "content": "Trend is up.",
                "trend": "up",
                "support": 95.0,
                "resistance": 120.0,
                "signals": ["fixture uptrend"],
                "confidence": 75,
            },
            "fundamentals_analyst": {
                "content": "Fundamentals are fair.",
                "valuation": "fair",
                "quality_flags": ["margin"],
                "red_flags": [],
                "upcoming_catalysts": [],
                "confidence": 70,
            },
            "news_analyst": {
                "content": "News is positive.",
                "sentiment": "positive",
                "key_events": ["demand update"],
                "macro_context": "No macro issue in fixture.",
                "confidence": 65,
            },
            "sentiment_analyst": {
                "content": "Mood is positive.",
                "retail_mood": "positive",
                "notable_narratives": ["AI demand"],
                "confidence": 60,
            },
            "bull_researcher": {"argument": "Bull case cites fixture uptrend."},
            "bear_researcher": {"argument": "Bear case cites valuation risk."},
            "debate_convergence_classifier": {"answer": "no"},
            "research_manager": {
                "content": "Bullish but size-aware.",
                "stance": "bullish",
                "conviction": 76,
                "thesis": "Fixture trend and news support a trade.",
                "key_risks": ["valuation"],
                "invalidation": "Break below support.",
                "debate_scorecard": "Bull trend was decisive; bear valuation capped size.",
            },
            "trader": {
                "content": f"{action} based on plan.",
                "action": action,
                "quantity_pct": quantity,
                "order_type": "market",
                "time_horizon_days": 10 if action != "HOLD" else 0,
                "entry_rationale": "Use deterministic fixture plan.",
                "exit_plan": "Exit on invalidation.",
                "stop_loss_pct": 5.0 if action != "HOLD" else None,
                "take_profit_pct": 12.0 if action != "HOLD" else None,
            },
            "aggressive_risk": {"argument": "Approve upside."},
            "conservative_risk": {"argument": "Watch downside."},
            "neutral_risk": {"argument": "Balanced size is acceptable."},
            "portfolio_manager": {
                "content": f"{verdict} at fixture size.",
                "verdict": verdict,
                "approved_quantity_pct": quantity if verdict != "REJECT" else 0.0,
                "reasoning": "Fixture PM decision.",
                "lessons_applied": [],
            },
        }
    )


def runner(tmp_path: Path, *, llm: FakeLLM | None = None, mandate_value: Mandate | None = None) -> OrchestratorRunner:
    bus = EventBus()
    return OrchestratorRunner(
        bus=bus,
        conn=conn(tmp_path),
        settings=settings(),
        mandate=mandate_value or mandate(),
        llm=llm or fake_llm(),
        router=FixtureRouter(),  # type: ignore[arg-type]
    )

"""Registry of pipelines that can be compared through the shared evaluation harness.

Every pipeline is described by a :class:`PipelineSpec` and, when built for an experiment,
yields a :class:`BuiltPipeline` — either a rule ``Strategy`` (evaluated every bar) or an
async agent callable (evaluated on the experiment cadence). Both are executed by the same
backtest engine on the same bars with the same fill model, so their metric rows are
directly comparable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from sentinel.backtest.strategies.base import BarContext, Strategy

if TYPE_CHECKING:
    from sentinel.eval.experiment import ExperimentContext

PipelineKind = Literal["rule", "agent"]


@dataclass
class InstrumentedStrategy:
    """Wrap a rule ``Strategy`` to count decisions/signals and time each ``on_bar``."""

    inner: Strategy
    decisions: int = 0
    signals: int = 0
    wall_ms_total: int = 0

    def on_bar(self, context: BarContext) -> Any:
        started = time.perf_counter()
        decision = self.inner.on_bar(context)
        self.wall_ms_total += int((time.perf_counter() - started) * 1000)
        self.decisions += 1
        if getattr(decision, "action", "HOLD") != "HOLD":
            self.signals += 1
        return decision

    def summary(self) -> dict[str, Any]:
        return {
            "decisions": self.decisions,
            "signals": self.signals,
            "failed_decisions": 0,
            "avg_wall_ms": (self.wall_ms_total / self.decisions) if self.decisions else 0.0,
        }


@dataclass
class BuiltPipeline:
    """A pipeline instantiated for one (experiment, symbol) run."""

    kind: PipelineKind
    strategy: InstrumentedStrategy | None = None
    agent: Callable[..., Any] | None = None
    summarize: Callable[[], dict[str, Any]] = field(default=lambda: {})
    # Extra per-run notes (e.g. failed decisions) surfaced in the results table.
    notes: Callable[[], list[str]] = field(default=list)


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    kind: PipelineKind
    description: str
    # Config that distinguishes this pipeline row in the results table.
    config: dict[str, Any]
    build: Callable[[ExperimentContext, str], BuiltPipeline]


def _build_sentinel(*, debate_enabled: bool) -> Callable[[ExperimentContext, str], BuiltPipeline]:
    def build(ctx: ExperimentContext, symbol: str) -> BuiltPipeline:
        from sentinel.eval.sentinel_adapter import SentinelPipelineAdapter, summarize_traces
        from sentinel.orchestrator.runner import OrchestratorRunner

        settings = ctx.settings.model_copy(
            update={"pipeline": ctx.settings.pipeline.model_copy(update={"debate_enabled": debate_enabled})}
        )
        runner = OrchestratorRunner(
            bus=ctx.bus,
            conn=ctx.conn,
            settings=settings,
            mandate=ctx.mandate,
            llm=ctx.llm,
            router=ctx.router,
        )
        adapter = SentinelPipelineAdapter(runner=runner, mandate=ctx.mandate, equity_hint=ctx.starting_cash)
        return BuiltPipeline(
            kind="agent",
            agent=adapter,
            summarize=lambda: summarize_traces(adapter.traces),
            notes=lambda: [
                f"{t.as_of}: {t.status} {t.error}" for t in adapter.traces if t.status == "failed" or t.error
            ],
        )

    return build


def _build_rule(factory: Callable[[dict[str, Any]], Strategy]) -> Callable[[ExperimentContext, str], BuiltPipeline]:
    def build(ctx: ExperimentContext, symbol: str) -> BuiltPipeline:
        params = dict(ctx.strategy_params or {})
        strategy = InstrumentedStrategy(factory(params))
        return BuiltPipeline(kind="rule", strategy=strategy, summarize=strategy.summary)

    return build


def _sma_cross(params: dict[str, Any]) -> Strategy:
    from sentinel.backtest.strategies.sma_cross import SMACrossStrategy

    return SMACrossStrategy(**{k: v for k, v in params.items() if k in {"short_window", "long_window", "size"}})


def _buy_hold(params: dict[str, Any]) -> Strategy:
    from sentinel.backtest.strategies.buy_hold import BuyHoldStrategy

    return BuyHoldStrategy(**{k: v for k, v in params.items() if k in {"size"}})


def _build_finrl(ctx: ExperimentContext, symbol: str) -> BuiltPipeline:
    """FinRL seam: a policy (trained elsewhere) drives the engine bar-by-bar; stubbed if absent."""

    from sentinel.eval.finrl_adapter import FinRLStrategy, load_policy

    params = dict(ctx.strategy_params or {})
    inner = FinRLStrategy(
        policy=load_policy(params.get("finrl_policy")),
        warmup_bars=int(params.get("finrl_warmup_bars", 50)),
    )
    strategy = InstrumentedStrategy(inner)

    def summarize() -> dict[str, Any]:
        summary = strategy.summary()
        summary["policy"] = inner.policy.name
        summary["stub"] = inner.is_stub
        return summary

    return BuiltPipeline(kind="rule", strategy=strategy, summarize=summarize, notes=inner.notes)


PIPELINES: dict[str, PipelineSpec] = {
    "sentinel_debate_on": PipelineSpec(
        name="sentinel_debate_on",
        kind="agent",
        description="Sentinel 11-node multi-agent pipeline with the bull/bear research debate enabled.",
        config={"debate_enabled": True},
        build=_build_sentinel(debate_enabled=True),
    ),
    "sentinel_debate_off": PipelineSpec(
        name="sentinel_debate_off",
        kind="agent",
        description="Sentinel pipeline with the research debate bypassed (deterministic analyst roll-up).",
        config={"debate_enabled": False},
        build=_build_sentinel(debate_enabled=False),
    ),
    "sma_cross": PipelineSpec(
        name="sma_cross",
        kind="rule",
        description="Rule baseline: SMA crossover (short/long window from strategy params).",
        config={"strategy": "sma_cross"},
        build=_build_rule(_sma_cross),
    ),
    "buy_hold": PipelineSpec(
        name="buy_hold",
        kind="rule",
        description="Rule baseline: buy on the first bar and hold.",
        config={"strategy": "buy_hold"},
        build=_build_rule(_buy_hold),
    ),
    "finrl": PipelineSpec(
        name="finrl",
        kind="rule",
        description=(
            "FinRL deep-RL policy behind the shared interface (sentinel/eval/finrl_adapter.py). "
            "Runs as a HOLD-only stub flagged in `notes` until a trained policy is supplied via "
            "FINRL_POLICY_PATH or strategy_params['finrl_policy']."
        ),
        config={"strategy": "finrl"},
        build=_build_finrl,
    ),
}


def get_pipeline(name: str) -> PipelineSpec:
    try:
        return PIPELINES[name]
    except KeyError as exc:
        msg = f"unknown pipeline {name!r}; available: {sorted(PIPELINES)}"
        raise ValueError(msg) from exc

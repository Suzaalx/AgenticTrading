"""Adapter that exposes the Sentinel multi-agent pipeline behind the backtest engine seam.

The engine hands us a point-in-time ``DataSnapshot``; we run it through the orchestrator
graph up to the Portfolio Manager and turn (trade proposal, PM verdict) into a ``Signal``
sized exactly the way the mandate gate would size a live paper order
(:func:`sentinel.risk.sizing.target_notional`). Fills are then simulated by the engine on
the next bar's open, identical to the rule baselines.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sentinel.core.models import DataSnapshot, Mandate, OptionChainSnapshot, RunState, Signal
from sentinel.orchestrator.runner import OrchestratorRunner
from sentinel.risk.sizing import target_notional

STOP_NODE = "portfolio_manager"


@dataclass
class DecisionTrace:
    """One pipeline decision as seen by the harness (for metrics + notes)."""

    run_id: str
    as_of: str
    action: str
    size: float | None
    verdict: str | None
    status: str
    error: str | None
    wall_ms: int


@dataclass
class SentinelPipelineAdapter:
    """Callable satisfying ``sentinel.backtest.engine.AgentPipeline``."""

    runner: OrchestratorRunner
    mandate: Mandate
    equity_hint: float
    traces: list[DecisionTrace] = field(default_factory=list)
    # run_id -> completed decision loaded from a previous run of the same experiment.
    cached: dict[str, RunState] = field(default_factory=dict)
    reused: int = 0

    async def __call__(
        self, snapshot: DataSnapshot, *, option_chain: OptionChainSnapshot | None = None
    ) -> Signal:
        started = time.perf_counter()
        cached_state = self.cached.get(snapshot.run_id)
        if cached_state is not None:
            # Decisions depend only on their point-in-time snapshot, so a stored
            # completed decision is exactly what a fresh call would have been fed.
            self.reused += 1
            signal = decision_to_signal(cached_state, mandate=self.mandate, equity=self.equity_hint)
            self.traces.append(
                DecisionTrace(
                    run_id=snapshot.run_id,
                    as_of=snapshot.as_of.date().isoformat(),
                    action=signal.action,
                    size=signal.size,
                    verdict=cached_state.pm_decision.verdict if cached_state.pm_decision else None,
                    status="completed",
                    error=None,
                    wall_ms=_stored_llm_ms(self.runner.conn, snapshot.run_id),
                )
            )
            return signal
        try:
            state = await self.runner.decide_from_snapshot(
                snapshot, option_chain=option_chain, stop_after=STOP_NODE
            )
        except Exception as exc:  # one bad LLM call must not sink the whole experiment
            self.traces.append(
                DecisionTrace(
                    run_id=snapshot.run_id,
                    as_of=snapshot.as_of.date().isoformat(),
                    action="HOLD",
                    size=None,
                    verdict=None,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    wall_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            return Signal(action="HOLD", size=None)
        signal = decision_to_signal(state, mandate=self.mandate, equity=self.equity_hint)
        self.traces.append(
            DecisionTrace(
                run_id=state.run_id,
                as_of=snapshot.as_of.date().isoformat(),
                action=signal.action,
                size=signal.size,
                verdict=state.pm_decision.verdict if state.pm_decision else None,
                status=state.status,
                error=state.error,
                wall_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        return signal


def decision_to_signal(state: RunState, *, mandate: Mandate, equity: float) -> Signal:
    """Map a finished (or short-circuited) run state to an engine ``Signal``.

    - HOLD proposal, REJECT verdict, failed run, or zero sizing -> HOLD.
    - BUY  -> size = target_notional / equity  (fraction of equity, as the mandate gate sizes).
    - SELL -> size = 1.0 (close the position; the engine sells ``shares * size``).
    """

    proposal = state.trade_proposal
    decision = state.pm_decision
    if state.status == "failed" or proposal is None or proposal.action == "HOLD":
        return Signal(action="HOLD", size=None)
    if decision is None or decision.verdict == "REJECT":
        return Signal(action="HOLD", size=None)
    if proposal.action == "SELL":
        return Signal(action="SELL", size=1.0)
    notional = target_notional(
        equity=Decimal(str(max(equity, 0.0))),
        mandate=mandate,
        quantity_pct=proposal.quantity_pct,
        trader_quantity_pct=proposal.quantity_pct,
        approved_quantity_pct=decision.approved_quantity_pct,
    )
    if equity <= 0 or notional <= 0:
        return Signal(action="HOLD", size=None)
    size = float(notional) / float(equity)
    return Signal(action="BUY", size=max(0.0, min(1.0, size)))


def summarize_traces(traces: list[DecisionTrace]) -> dict[str, Any]:
    """Aggregate per-decision traces into the numbers the metrics table needs."""

    decisions = len(traces)
    signals = sum(1 for trace in traces if trace.action != "HOLD")
    failures = sum(1 for trace in traces if trace.status == "failed" or trace.error)
    wall = [trace.wall_ms for trace in traces]
    return {
        "decisions": decisions,
        "signals": signals,
        "failed_decisions": failures,
        "avg_wall_ms": (sum(wall) / decisions) if decisions else 0.0,
    }


def load_completed_decisions(conn: Any, bt_id: str) -> dict[str, RunState]:
    """Rebuild completed decisions (trader proposal + PM verdict, or trader HOLD) from ``reports``."""

    from sentinel.core.models import PMDecision, TradeProposal
    from sentinel.orchestrator.state import initial_state

    rows = conn.execute(
        "SELECT run_id, agent, structured_json FROM reports "
        "WHERE (run_id LIKE ?) AND agent IN ('trader', 'portfolio_manager') ORDER BY created_at",
        (f"{bt_id}:%",),
    ).fetchall()
    by_run: dict[str, dict[str, str]] = {}
    for row in rows:
        by_run.setdefault(row["run_id"], {})[row["agent"]] = row["structured_json"]
    completed: dict[str, RunState] = {}
    for run_id, agents in by_run.items():
        trader_json = agents.get("trader")
        if trader_json is None:
            continue
        try:
            proposal = TradeProposal.model_validate_json(trader_json)
        except Exception:  # an option proposal or malformed row: recompute it
            continue
        pm = agents.get("portfolio_manager")
        if proposal.action != "HOLD" and pm is None:
            continue  # stopped between trader and PM -> not complete
        state = initial_state("RESUMED", run_id=run_id)
        state.status = "completed"
        state.trade_proposal = proposal
        state.pm_decision = PMDecision.model_validate_json(pm) if pm is not None else None
        completed[run_id] = state
    return completed


def _stored_llm_ms(conn: Any, run_id: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(latency_ms), 0) AS ms FROM costs WHERE run_id = ?", (run_id,)).fetchone()
    return int(row["ms"] or 0)

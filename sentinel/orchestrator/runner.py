"""Async run orchestration with SQLite checkpoint/resume support."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sentinel.config.settings import Settings, load_mandate, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.models import Mandate, OptionChainSnapshot, RunState
from sentinel.data.router import DataRouter
from sentinel.execution.broker import QuoteSource
from sentinel.execution.paper import PaperBroker
from sentinel.execution.router import ExecutionRouter
from sentinel.llm.budget import assert_within_budget
from sentinel.llm.contracts import StructuredLLM
from sentinel.llm.gateway import LLMGateway
from sentinel.risk.audit import append_audit
from sentinel.risk.live_mandate import LiveMandate
from sentinel.store.db import connect, default_db_path, run_migrations
from sentinel.store.repos.runs_repo import RunRecord, insert_run

from .graph import NODE_ORDER, NodeName, OrchestratorGraph, RouterQuoteSource
from .state import deserialize_state, initial_state, run_dir, serialize_state


class RunCheckpointStore:
    """One SQLite checkpointer per run under ``SENTINEL_HOME/runs/<run_id>``."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.path = run_dir(run_id) / "checkpoint.db"
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cancel_flags (
                    run_id TEXT PRIMARY KEY,
                    cancelled INT NOT NULL
                )"""
            )

    async def save(self, state: RunState, node: NodeName) -> None:
        """Persist a completed node and full state JSON."""

        payload = serialize_state(state)
        await asyncio.to_thread(self._save_sync, node, payload)

    def _save_sync(self, node: NodeName, payload: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (run_id, node, state_json, completed_at) VALUES (?, ?, ?, ?)",
                (self.run_id, node, payload, datetime.now(UTC).isoformat()),
            )

    def latest(self) -> tuple[NodeName, RunState] | None:
        """Return the last completed node and state, if any."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT node, state_json FROM checkpoints WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                (self.run_id,),
            ).fetchone()
        if row is None:
            return None
        return row["node"], deserialize_state(row["state_json"])

    def cancel(self) -> None:
        """Set the cooperative cancellation flag for this run."""

        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cancel_flags (run_id, cancelled) VALUES (?, 1)",
                (self.run_id,),
            )

    def is_cancelled(self) -> bool:
        """Return whether this run has been cancelled."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancelled FROM cancel_flags WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()
        return bool(row["cancelled"]) if row is not None else False


class OrchestratorRunner:
    """High-level async runner used by the CLI, service, tests, and backtest seam."""

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        conn: sqlite3.Connection | None = None,
        settings: Settings | None = None,
        mandate: Mandate | None = None,
        llm: StructuredLLM | None = None,
        router: DataRouter | None = None,
        quote_source: QuoteSource | None = None,
        broker: PaperBroker | None = None,
        execution_router: ExecutionRouter | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.bus = bus or EventBus()
        self.settings = settings or load_settings(Path.cwd())
        self.mandate = mandate or load_mandate(Path.cwd())
        self.conn = conn or connect(db_path or default_db_path())
        run_migrations(self.conn)
        self.llm = llm or LLMGateway(settings=self.settings)
        self.router = router
        self.quote_source = quote_source
        self.broker = broker
        self.execution_router = execution_router
        self._active_lock = asyncio.Lock()

    async def run(
        self,
        symbol: str,
        *,
        as_of: datetime | None = None,
        depth: str = "standard",
        run_id: str | None = None,
        stop_after: NodeName | None = None,
    ) -> RunState:
        """Execute a new decision run to completion unless ``stop_after`` is supplied."""

        assert_within_budget(self.conn, Decimal(str(self.settings.llm.monthly_budget_usd)))
        max_rounds = self._rounds_for_depth(depth)
        state = initial_state(symbol, as_of=as_of, run_id=run_id)
        state.debate_enabled = self.settings.pipeline.debate_enabled
        store = RunCheckpointStore(state.run_id)
        self._persist_run(state)
        async with self._active_lock:
            graph = self._graph(store, max_rounds=max_rounds)
            try:
                state = await graph.run(state, stop_after=stop_after)
            except Exception as exc:
                state.status = "failed"
                state.error = str(exc)
                await store.save(state, "finalize")
                raise
            finally:
                self._persist_run(state)
        return state

    async def resume(self, run_id: str, *, depth: str = "standard") -> RunState:
        """Resume a run from the checkpoint after the last completed node."""

        store = RunCheckpointStore(run_id)
        latest = store.latest()
        if latest is None:
            msg = f"no checkpoint found for run {run_id}"
            raise FileNotFoundError(msg)
        last_node, state = latest
        if state.status in {"completed", "failed", "cancelled", "halted"}:
            return state
        next_node = self._next_node(last_node)
        max_rounds = self._rounds_for_depth(depth)
        async with self._active_lock:
            graph = self._graph(store, max_rounds=max_rounds)
            state = await graph.run(state, start_at=next_node)
            self._persist_run(state)
        return state

    def cancel_run(self, run_id: str) -> None:
        """Request cooperative cancellation at the next node boundary."""

        RunCheckpointStore(run_id).cancel()

    async def run_backtest_step(
        self,
        snapshot,
        *,
        option_chain: OptionChainSnapshot | None = None,
    ) -> object:
        """Agent-replay seam: run a single supplied snapshot through post-data nodes."""

        state = initial_state(snapshot.symbol, as_of=snapshot.as_of, run_id=snapshot.run_id, mode="backtest_step")
        state.debate_enabled = self.settings.pipeline.debate_enabled
        state.snapshot = snapshot
        state.option_chain = option_chain
        store = RunCheckpointStore(state.run_id.replace(":", "_"))
        graph = self._graph(store, max_rounds=self.settings.pipeline.max_debate_rounds)
        state = await graph.run(state, start_at="analysts")
        return state.option_proposal or state.trade_proposal

    def _graph(self, store: RunCheckpointStore, *, max_rounds: int) -> OrchestratorGraph:
        return OrchestratorGraph(
            llm=self.llm,
            bus=self.bus,
            conn=self.conn,
            settings=self.settings,
            mandate=self.mandate,
            router=self.router,
            quote_source=self.quote_source,
            broker=self.broker,
            execution_router=self._execution_router(),
            max_debate_rounds=max_rounds,
            max_risk_rounds=max(1, min(max_rounds, self.settings.pipeline.max_risk_discuss_rounds)),
            debate_enabled=self.settings.pipeline.debate_enabled,
            checkpoint=store.save,
            cancel_check=lambda run_id: RunCheckpointStore(run_id).is_cancelled(),
        )

    def _execution_router(self) -> ExecutionRouter:
        if self.execution_router is not None:
            return self.execution_router
        quote_source = self.quote_source or RouterQuoteSource(self.router or DataRouter(settings=self.settings))
        broker = self.broker or PaperBroker(quote_source, bus=self.bus)
        self.execution_router = ExecutionRouter(
            paper=broker,
            crypto=None,
            agentic=None,
            live_mandate=LiveMandate(self.mandate),
            audit=append_audit,
        )
        return self.execution_router

    def _rounds_for_depth(self, depth: str) -> int:
        return int(self.settings.pipeline.depth_presets.get(depth, self.settings.pipeline.max_debate_rounds))

    @staticmethod
    def _next_node(last_node: NodeName) -> NodeName:
        index = NODE_ORDER.index(last_node)
        if index + 1 >= len(NODE_ORDER):
            return "finalize"
        return NODE_ORDER[index + 1]

    def _persist_run(self, state: RunState) -> None:
        insert_run(
            self.conn,
            RunRecord(
                run_id=state.run_id,
                symbol=state.symbol,
                as_of=state.as_of,
                mode=state.mode,
                status=state.status,
                action=state.trade_proposal.action if state.trade_proposal is not None else None,
                verdict=state.pm_decision.verdict if state.pm_decision is not None else None,
                cost_usd=state.total_cost_usd,
                tokens=state.total_tokens,
                debate_enabled=state.debate_enabled,
                finished_at=datetime.now(UTC)
                if state.status in {"completed", "failed", "cancelled", "halted"}
                else None,
            ),
        )

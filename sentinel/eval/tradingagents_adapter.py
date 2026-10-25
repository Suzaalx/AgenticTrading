"""TradingAgents (Xiao et al., 2024) behind the shared evaluation harness.

The published framework (https://github.com/TauricResearch/TradingAgents) runs unmodified;
this adapter only:

- calls ``TradingAgentsGraph.propagate(ticker, date)`` once per harness decision date
  (TradingAgents fetches its own point-in-time data; its date-window guards clamp every
  request to the trade date, so it cannot see the future);
- maps its 5-tier rating onto an engine ``Signal`` sized with the same mandate allowance
  Sentinel uses, so fills, slippage and metrics are identical to every other pipeline;
- meters every LLM call into Sentinel's ``costs`` table (tokens, latency) under the
  decision's ``run_id``, so cost-per-decision is computed the same way;
- keeps TradingAgents' own files (results, cache, memory log) inside the experiment
  directory, so runs never share memory across experiments.

Install with ``uv sync --extra tradingagents``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel.core.models import DataSnapshot, Mandate, OptionChainSnapshot, Signal
from sentinel.eval.sentinel_adapter import DecisionTrace
from sentinel.llm.cost import cost_usd, record_cost

# Sentinel provider name -> (TradingAgents provider name, env var TradingAgents reads for the key)
PROVIDER_MAP: dict[str, tuple[str, str | None]] = {
    "groq": ("groq", "GROQ_API_KEY"),
    "gemini": ("google", "GOOGLE_API_KEY"),
    "ollama": ("ollama", None),
    "openai": ("openai", "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
}
# Share of the mandate allowance (max_position_pct_equity) each rating buys or sells.
RATING_ACTIONS: dict[str, tuple[str, float]] = {
    "buy": ("BUY", 1.0),
    "overweight": ("BUY", 0.5),
    "hold": ("HOLD", 0.0),
    "underweight": ("SELL", 0.5),
    "sell": ("SELL", 1.0),
}


def rating_to_signal(rating: str, *, mandate: Mandate) -> Signal:
    """Map a TradingAgents rating to an engine Signal.

    BUY sizes are a fraction of the mandate's per-position allowance (10% of equity by
    default) - the same cap that bounds Sentinel - so exposure is comparable. SELL sizes
    are the fraction of the current position to close. Unreadable ratings ("REVIEW") hold.
    """

    action, fraction = RATING_ACTIONS.get(str(rating).strip().lower(), ("HOLD", 0.0))
    if action == "BUY":
        return Signal(action="BUY", size=fraction * mandate.max_position_pct_equity / 100.0)
    if action == "SELL":
        return Signal(action="SELL", size=fraction)
    return Signal(action="HOLD", size=None)


def tradingagents_config(
    *,
    provider: str,
    quick_model: str,
    deep_model: str,
    workdir: Path,
    max_debate_rounds: int = 1,
    max_risk_rounds: int = 1,
    max_retries: int = 12,
) -> dict[str, Any]:
    """Build a TradingAgents config from Sentinel's LLM settings (same models, isolated files)."""

    from tradingagents.default_config import DEFAULT_CONFIG

    if provider not in PROVIDER_MAP:
        msg = f"TradingAgents adapter has no mapping for provider {provider!r}; known: {sorted(PROVIDER_MAP)}"
        raise ValueError(msg)
    ta_provider, key_env = PROVIDER_MAP[provider]
    if provider == "gemini" and key_env and not os.environ.get(key_env) and os.environ.get("GEMINI_API_KEY"):
        os.environ[key_env] = os.environ["GEMINI_API_KEY"]  # TradingAgents reads GOOGLE_API_KEY
    config = dict(DEFAULT_CONFIG)
    config.update(
        llm_provider=ta_provider,
        quick_think_llm=quick_model,
        deep_think_llm=deep_model,
        max_debate_rounds=max_debate_rounds,
        max_risk_discuss_rounds=max_risk_rounds,
        llm_max_retries=max_retries,
        checkpoint_enabled=False,
        results_dir=str(workdir / "results"),
        data_cache_dir=str(workdir / "cache"),
        memory_log_path=str(workdir / "memory" / "trading_memory.md"),
    )
    return config


class _CostMeter:
    """LangChain callback recording one Sentinel ``costs`` row per TradingAgents LLM call.

    TradingAgents runs in a worker thread and SQLite connections are bound to the thread
    that opened them, so callbacks only buffer rows (thread-safe); ``flush()`` writes them
    from the caller's thread after each decision.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.run_id: str | None = None
        self._started: dict[Any, float] = {}
        self._pending: list[tuple[str | None, str, str, int, int, int]] = []
        self._lock = threading.Lock()
        self.calls = 0

    def make_handler(self) -> Any:
        from langchain_core.callbacks import BaseCallbackHandler

        meter = self

        class Handler(BaseCallbackHandler):
            def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):  # type: ignore[no-untyped-def]
                meter._started[run_id] = time.perf_counter()

            def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):  # type: ignore[no-untyped-def]
                meter._started[run_id] = time.perf_counter()

            def on_llm_end(self, response, *, run_id, **kwargs):  # type: ignore[no-untyped-def]
                started = meter._started.pop(run_id, None)
                latency_ms = int((time.perf_counter() - started) * 1000) if started else 0
                node = (kwargs.get("metadata") or {}).get("langgraph_node") or "tradingagents"
                for generations in response.generations:
                    for gen in generations:
                        message = getattr(gen, "message", None)
                        usage = getattr(message, "usage_metadata", None) or {}
                        meta = getattr(message, "response_metadata", None) or {}
                        model = str(meta.get("model_name") or meta.get("model") or "unknown")
                        meter.record(node, model, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), latency_ms)

        return Handler()

    def record(self, agent: str, model: str, tokens_in: int, tokens_out: int, latency_ms: int) -> None:
        with self._lock:
            self.calls += 1
            self._pending.append((self.run_id, agent, model, tokens_in, tokens_out, latency_ms))

    def flush(self) -> int:
        """Write buffered rows on the calling thread; returns how many were written."""

        with self._lock:
            rows, self._pending = self._pending, []
        for run_id, agent, model, tokens_in, tokens_out, latency_ms in rows:
            record_cost(
                self.conn,
                run_id=run_id,
                agent=f"ta:{agent}",
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost_usd(model, tokens_in, tokens_out),
                latency_ms=latency_ms,
            )
        return len(rows)


@dataclass
class TradingAgentsPipelineAdapter:
    """Callable satisfying ``sentinel.backtest.engine.AgentPipeline`` for TradingAgents."""

    graph_factory: Callable[[list[Any]], Any]
    symbol: str
    mandate: Mandate
    conn: sqlite3.Connection
    traces: list[DecisionTrace] = field(default_factory=list)
    _graph: Any = field(default=None, repr=False)
    _meter: _CostMeter | None = field(default=None, repr=False)

    async def __call__(self, snapshot: DataSnapshot, *, option_chain: OptionChainSnapshot | None = None) -> Signal:
        if self._graph is None:
            self._meter = _CostMeter(self.conn)
            self._graph = self.graph_factory([self._meter.make_handler()])
        assert self._meter is not None
        self._meter.run_id = snapshot.run_id
        trade_date = snapshot.as_of.date().isoformat()
        started = time.perf_counter()
        try:
            _state, rating = await asyncio.to_thread(self._graph.propagate, self.symbol, trade_date)
        except Exception as exc:  # one failed decision must not sink the experiment
            self._meter.flush()  # tokens spent before the failure still count
            self._trace(snapshot, "HOLD", None, "failed", f"{type(exc).__name__}: {exc}", started)
            return Signal(action="HOLD", size=None)
        self._meter.flush()
        signal = rating_to_signal(str(rating), mandate=self.mandate)
        error = None if str(rating).lower() in RATING_ACTIONS else f"UnreadableRating: {rating}"
        self._trace(snapshot, signal.action, signal.size, "completed" if error is None else "failed", error, started,
                    verdict=str(rating))
        return signal

    def _trace(self, snapshot: DataSnapshot, action: str, size: float | None, status: str, error: str | None,
               started: float, verdict: str | None = None) -> None:
        self.traces.append(
            DecisionTrace(
                run_id=snapshot.run_id,
                as_of=snapshot.as_of.date().isoformat(),
                action=action,
                size=size,
                verdict=verdict,
                status=status,
                error=error,
                wall_ms=int((time.perf_counter() - started) * 1000),
            )
        )


def default_graph_factory(config: dict[str, Any]) -> Callable[[list[Any]], Any]:
    """Factory building the real ``TradingAgentsGraph`` with our metering callbacks."""

    def build(callbacks: list[Any]) -> Any:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        return TradingAgentsGraph(debug=False, config=config, callbacks=callbacks)

    return build

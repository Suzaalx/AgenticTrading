"""Hand-rolled async Sentinel decision state machine."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from sentinel.agents.analysts import (
    FundamentalsAnalyst,
    MarketAnalyst,
    NewsAnalyst,
    SentimentAnalyst,
)
from sentinel.agents.portfolio_manager import PortfolioManager
from sentinel.agents.researchers import (
    BearResearcher,
    BullResearcher,
    ResearchManager,
    run_research_debate,
)
from sentinel.agents.risk_debaters import (
    AggressiveRisk,
    ConservativeRisk,
    NeutralRisk,
    run_risk_debate,
)
from sentinel.agents.trader import Trader
from sentinel.config.settings import Settings
from sentinel.core.bus import EventBus
from sentinel.core.events import (
    DecisionMade,
    EquityUpdated,
    GateEvaluated,
    RunStarted,
    StageChanged,
)
from sentinel.core.ids import new_id
from sentinel.core.models import (
    AgentReport,
    Fill,
    GateResult,
    InvestmentPlan,
    JournalEntry,
    Mandate,
    Order,
    PMDecision,
    Portfolio,
    RunState,
    TradeProposal,
)
from sentinel.data.router import DataRouter
from sentinel.data.snapshot import build_snapshot
from sentinel.execution.broker import OrderRejected, QuoteSource
from sentinel.execution.ledger import append_order, record_fill, update_order_status
from sentinel.execution.paper import PaperBroker
from sentinel.execution.portfolio import (
    append_equity_snapshot,
    apply_fill,
    load_portfolio,
    save_positions,
)
from sentinel.llm.contracts import StructuredLLM
from sentinel.memory.journal import write_journal_entry
from sentinel.memory.recall import recall_lessons
from sentinel.risk.gate import check_order
from sentinel.risk.killswitch import is_engaged
from sentinel.risk.sizing import size_order

NodeName = Literal[
    "fetch_data",
    "analysts",
    "research_debate",
    "research_manager",
    "trader",
    "risk_debate",
    "portfolio_manager",
    "mandate_gate",
    "execute",
    "reflect_enqueue",
    "finalize",
]
CheckpointCallback = Callable[[RunState, NodeName], Awaitable[None]]
CancelCheck = Callable[[str], bool]

NODE_ORDER: tuple[NodeName, ...] = (
    "fetch_data",
    "analysts",
    "research_debate",
    "research_manager",
    "trader",
    "risk_debate",
    "portfolio_manager",
    "mandate_gate",
    "execute",
    "reflect_enqueue",
    "finalize",
)


class RouterQuoteSource(QuoteSource):
    """Adapt ``DataRouter.get_quote`` to the broker ``QuoteSource`` protocol."""

    def __init__(self, router: DataRouter) -> None:
        self.router = router

    def get_quote(self, symbol: str):
        """Return the router's latest quote or ``None``."""

        return self.router.get_quote(symbol)


def _debate_tags(winner: Literal["bull", "bear", "split"]) -> list[str]:
    label = {
        "bull": "bull_won_debate",
        "bear": "bear_won_debate",
        "split": "debate_split",
    }[winner]
    return [f"debate_won_by:{winner}", label]


class OrchestratorGraph:
    """Dependency-injected async state machine for one decision run."""

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        bus: EventBus,
        conn: sqlite3.Connection,
        settings: Settings,
        mandate: Mandate,
        router: DataRouter | None = None,
        quote_source: QuoteSource | None = None,
        broker: PaperBroker | None = None,
        max_debate_rounds: int | None = None,
        max_risk_rounds: int | None = None,
        checkpoint: CheckpointCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        self.llm = llm
        self.bus = bus
        self.conn = conn
        self.settings = settings
        self.mandate = mandate
        self.router = router or DataRouter(settings=settings)
        self.quote_source = quote_source or RouterQuoteSource(self.router)
        self.broker = broker or PaperBroker(self.quote_source, bus=bus)
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_rounds = max_risk_rounds
        self.checkpoint = checkpoint
        self.cancel_check = cancel_check or (lambda _run_id: False)

    async def run(
        self,
        state: RunState,
        *,
        start_at: NodeName = "fetch_data",
        stop_after: NodeName | None = None,
    ) -> RunState:
        """Run nodes from ``start_at`` through finalize, checkpointing between nodes."""

        if start_at == "fetch_data":
            await self.bus.publish(
                RunStarted(run_id=state.run_id, symbol=state.symbol, as_of=state.as_of, mode=state.mode)
            )
        start_index = NODE_ORDER.index(start_at)
        for node in NODE_ORDER[start_index:]:
            if self.cancel_check(state.run_id):
                state.status = "cancelled"
                await self._stage(state, "cancelled")
                await self._checkpoint(state, node)
                break
            await self._run_node(node, state)
            await self._checkpoint(state, node)
            if stop_after == node or state.status in {"completed", "failed", "cancelled", "halted"}:
                break
        return state

    async def _run_node(self, node: NodeName, state: RunState) -> None:
        match node:
            case "fetch_data":
                await self._fetch_data(state)
            case "analysts":
                await self._analysts(state)
            case "research_debate":
                await self._research_debate(state)
            case "research_manager":
                await self._research_manager(state)
            case "trader":
                await self._trader(state)
            case "risk_debate":
                await self._risk_debate(state)
            case "portfolio_manager":
                await self._portfolio_manager(state)
            case "mandate_gate":
                await self._mandate_gate(state)
            case "execute":
                await self._execute(state)
            case "reflect_enqueue":
                await self._reflect_enqueue(state)
            case "finalize":
                await self._finalize(state)

    async def _fetch_data(self, state: RunState) -> None:
        if is_engaged():
            state.status = "halted"
            await self._stage(state, "fetch_data")
            return
        state.status = "fetching_data"
        await self._stage(state, "fetch_data")
        state.snapshot = await asyncio.to_thread(
            build_snapshot,
            self.router,
            state.symbol,
            state.as_of,
            state.run_id,
            self.settings,
        )

    async def _analysts(self, state: RunState) -> None:
        if state.status == "halted":
            return
        state.status = "analyzing"
        await self._stage(state, "analysts")
        agents = (
            MarketAnalyst(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings),
            FundamentalsAnalyst(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings),
            NewsAnalyst(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings),
            SentimentAnalyst(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings),
        )
        reports = await asyncio.gather(*(agent.run(state) for agent in agents))
        for report in reports:
            state.analyst_reports[report.agent] = report
            self._persist_report(report)
        self._rollup_costs(state)

    async def _research_debate(self, state: RunState) -> None:
        state.status = "debating"
        await self._stage(state, "research_debate")
        lessons = self._recall_lessons(state)
        await run_research_debate(
            state,
            BullResearcher(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings, lessons=lessons),
            BearResearcher(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings, lessons=lessons),
            max_rounds=self.max_debate_rounds,
            lessons=lessons,
        )
        self._rollup_costs(state)

    async def _research_manager(self, state: RunState) -> None:
        state.status = "debating"
        await self._stage(state, "research_manager")
        lessons = self._recall_lessons(state)
        plan = cast(
            InvestmentPlan,
            await ResearchManager(
                llm=self.llm,
                bus=self.bus,
                conn=self.conn,
                settings=self.settings,
                lessons=lessons,
            ).run(state),
        )
        state.investment_plan = plan
        self._persist_report(plan)
        self._rollup_costs(state)

    async def _trader(self, state: RunState) -> None:
        state.status = "trading"
        await self._stage(state, "trader")
        portfolio = load_portfolio(self.conn, starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)))
        lessons = self._recall_lessons(state)
        proposal = cast(
            TradeProposal,
            await Trader(
                llm=self.llm,
                bus=self.bus,
                conn=self.conn,
                settings=self.settings,
                portfolio=portfolio,
                lessons=lessons,
            ).run(state),
        )
        state.trade_proposal = proposal
        self._persist_report(proposal)
        self._rollup_costs(state)
        if proposal.action == "HOLD":
            state.final_order = None

    async def _risk_debate(self, state: RunState) -> None:
        if state.trade_proposal is None or (
            state.trade_proposal.action == "HOLD" and not self.settings.pipeline.always_run_risk_debate
        ):
            return
        state.status = "risk_review"
        await self._stage(state, "risk_debate")
        portfolio = load_portfolio(self.conn, starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)))
        await run_risk_debate(
            state,
            AggressiveRisk(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings, portfolio=portfolio),
            ConservativeRisk(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings, portfolio=portfolio),
            NeutralRisk(llm=self.llm, bus=self.bus, conn=self.conn, settings=self.settings, portfolio=portfolio),
            max_rounds=self.max_risk_rounds,
        )
        self._rollup_costs(state)

    async def _portfolio_manager(self, state: RunState) -> None:
        if state.trade_proposal is None or state.trade_proposal.action == "HOLD":
            return
        state.status = "risk_review"
        await self._stage(state, "portfolio_manager")
        portfolio = load_portfolio(self.conn, starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)))
        lessons = self._recall_lessons(state)
        decision = cast(
            PMDecision,
            await PortfolioManager(
                llm=self.llm,
                bus=self.bus,
                conn=self.conn,
                settings=self.settings,
                portfolio=portfolio,
                lessons=lessons,
            ).run(state),
        )
        state.pm_decision = decision
        self._persist_report(decision)
        self._rollup_costs(state)
        await self.bus.publish(DecisionMade(run_id=state.run_id, decision=decision))
        if decision.verdict == "REJECT":
            state.final_order = None

    async def _mandate_gate(self, state: RunState) -> None:
        if state.trade_proposal is None or state.trade_proposal.action == "HOLD":
            return
        if state.pm_decision is None or state.pm_decision.verdict == "REJECT":
            return
        state.status = "gating"
        await self._stage(state, "mandate_gate")
        quote = await self._get_quote(state.symbol)
        if quote is None or quote.price <= 0:
            result = GateResult(
                passed=False,
                violations=[],
            )
            state.final_order = None
            state.error = f"no executable quote for {state.symbol}"
            await self.bus.publish(GateEvaluated(run_id=state.run_id, result=result))
            return
        marks = {state.symbol: quote.price}
        portfolio = load_portfolio(self.conn, starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)))
        equity = portfolio.marked_equity(marks)
        qty = size_order(
            symbol=state.symbol,
            equity=equity,
            price=quote.price,
            mandate=self.mandate,
            quantity_pct=state.trade_proposal.quantity_pct,
            trader_quantity_pct=state.trade_proposal.quantity_pct,
            approved_quantity_pct=state.pm_decision.approved_quantity_pct,
        )
        if qty <= 0:
            state.final_order = None
            state.error = "sized order quantity was zero"
            result = GateResult(passed=False, violations=[])
            await self.bus.publish(GateEvaluated(run_id=state.run_id, result=result))
            return
        order = Order(
            order_id=new_id(),
            run_id=state.run_id,
            symbol=state.symbol,
            side="buy" if state.trade_proposal.action == "BUY" else "sell",
            qty=qty,
            type="market",
            reason="agent_decision",
            created_at=datetime.now(UTC),
        )
        result = check_order(
            order,
            portfolio,
            self.mandate,
            orders_today=self._orders_today(order.created_at),
            last_order_ts=self._last_order_ts_by_symbol(),
            kill_engaged=is_engaged(),
            marks=marks,
            day_pnl_pct=None,
        )
        await self.bus.publish(GateEvaluated(run_id=state.run_id, result=result))
        if not result.passed:
            state.final_order = None
            state.error = "gate veto: " + ", ".join(v.code for v in result.violations)
            return
        state.final_order = order

    async def _execute(self, state: RunState) -> None:
        if state.final_order is None:
            return
        if is_engaged():
            state.status = "halted"
            await self._stage(state, "execute")
            return
        state.status = "executing"
        await self._stage(state, "execute")
        append_order(self.conn, state.final_order, status="submitted")
        result = await self.broker.submit(state.final_order)
        if isinstance(result, OrderRejected):
            update_order_status(self.conn, state.final_order.order_id, "rejected")
            state.final_order = None
            state.error = result.reason
            return
        fill = cast(Fill, result)
        record_fill(self.conn, state.final_order, fill)
        portfolio = load_portfolio(self.conn, starting_cash=Decimal(str(self.settings.execution.starting_cash_usd)))
        updated, _realized_pnl = apply_fill(portfolio, state.final_order, fill)
        self._apply_position_terms(updated, state)
        save_positions(self.conn, updated)
        equity = append_equity_snapshot(self.conn, updated, marks={state.symbol: fill.price})
        await self.bus.publish(EquityUpdated(equity=equity.equity, cash=equity.cash, day_pnl=equity.day_pnl))

    async def _reflect_enqueue(self, state: RunState) -> None:
        if state.trade_proposal is None:
            return
        await self._stage(state, "reflect_enqueue")
        plan = state.investment_plan
        horizon_end = (
            state.as_of.date() + timedelta(days=state.trade_proposal.time_horizon_days)
            if state.trade_proposal.time_horizon_days > 0
            else None
        )
        entry = JournalEntry(
            run_id=state.run_id,
            symbol=state.symbol,
            date=state.as_of.date(),
            stance=plan.stance if plan is not None else "neutral",
            action=state.trade_proposal.action,
            conviction=plan.conviction if plan is not None else 0,
            size=state.trade_proposal.quantity_pct,
            thesis_summary=(plan.thesis if plan is not None else state.trade_proposal.entry_rationale)[:1000],
            invalidation=plan.invalidation if plan is not None else state.trade_proposal.exit_plan,
            horizon_end=horizon_end,
        )
        write_journal_entry(self.conn, entry)

    async def _finalize(self, state: RunState) -> None:
        if state.status not in {"cancelled", "halted", "failed"}:
            state.status = "completed"
        await self._stage(state, "finalize")

    async def _stage(self, state: RunState, stage: str) -> None:
        await self.bus.publish(StageChanged(run_id=state.run_id, stage=stage, status=state.status))

    async def _checkpoint(self, state: RunState, node: NodeName) -> None:
        if self.checkpoint is not None:
            await self.checkpoint(state, node)

    def _recall_lessons(self, state: RunState):
        tags = self._setup_tags(state)
        return recall_lessons(self.conn, state.symbol, tags, limit=5)

    @staticmethod
    def _setup_tags(state: RunState) -> list[str]:
        tags: list[str] = []
        if state.investment_plan is not None:
            tags.append(state.investment_plan.stance)
            tags.extend(_debate_tags(state.investment_plan.debate_won_by))
            tags.extend(risk.lower().replace(" ", "_") for risk in state.investment_plan.key_risks[:3])
        for report in state.analyst_reports.values():
            tags.extend(str(signal).lower().replace(" ", "_") for signal in getattr(report, "signals", [])[:2])
        return tags

    async def _get_quote(self, symbol: str):
        maybe_quote = self.quote_source.get_quote(symbol)
        if inspect.isawaitable(maybe_quote):
            return await maybe_quote
        return maybe_quote

    def _persist_report(self, report: AgentReport) -> None:
        self.conn.execute(
            """INSERT INTO reports
            (run_id, agent, model, content, structured_json, tokens_in, tokens_out, cost_usd, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report.run_id,
                report.agent,
                report.model,
                report.content,
                report.model_dump_json(),
                report.input_tokens,
                report.output_tokens,
                str(report.cost_usd),
                report.latency_ms,
                report.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def _rollup_costs(self, state: RunState) -> None:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens FROM costs WHERE run_id = ?",
            (state.run_id,),
        ).fetchone()
        if row is not None:
            state.total_cost_usd = Decimal(str(row["cost"]))
            state.total_tokens = int(row["tokens"])

    def _orders_today(self, now: datetime) -> int:
        prefix = now.date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE substr(created_at, 1, 10) = ?",
            (prefix,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _last_order_ts_by_symbol(self) -> dict[str, datetime]:
        rows = self.conn.execute(
            "SELECT symbol, MAX(created_at) AS ts FROM orders GROUP BY symbol",
        ).fetchall()
        return {
            str(row["symbol"]): datetime.fromisoformat(str(row["ts"]))
            for row in rows
            if row["ts"] is not None
        }

    @staticmethod
    def _apply_position_terms(portfolio: Portfolio, state: RunState) -> None:
        if state.trade_proposal is None or state.final_order is None:
            return
        for index, position in enumerate(portfolio.positions):
            if position.symbol == state.final_order.symbol:
                portfolio.positions[index] = position.model_copy(
                    update={
                        "stop_loss_pct": state.trade_proposal.stop_loss_pct,
                        "take_profit_pct": state.trade_proposal.take_profit_pct,
                        "horizon_days": state.trade_proposal.time_horizon_days,
                        "source_run_id": state.run_id,
                    }
                )
                return

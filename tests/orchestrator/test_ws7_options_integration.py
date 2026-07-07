from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.agents.reflection import _grade_option_entry
from sentinel.core.bus import EventBus
from sentinel.core.events import AgentCompleted, GateEvaluated, OrderFilled
from sentinel.core.models import Mandate, OptionChainSnapshot, OptionContract, OptionQuote
from sentinel.data.loaders.rates import RateResult
from sentinel.data.loaders.yfinance_options import LoadedOptionChain
from sentinel.data.router import DataRouter
from sentinel.memory.journal import get_journal_entry
from sentinel.orchestrator.runner import OrchestratorRunner
from sentinel.risk.killswitch import engage
from sentinel.store.db import sentinel_home

from ._helpers import FixtureRouter, conn, fake_llm, mandate, settings


class OptionFixtureRouter(FixtureRouter):
    def __init__(self) -> None:
        super().__init__(price=Decimal("100"))
        self.chain_calls = 0
        self._chain: OptionChainSnapshot | None = None

    def get_option_chain(self, underlying: str, as_of: datetime) -> OptionChainSnapshot:
        self.chain_calls += 1
        self._chain = _option_chain(underlying, as_of)
        return self._chain

    def get_option_quote(self, contract: OptionContract) -> OptionQuote | None:
        if self._chain is None:
            self._chain = _option_chain(contract.underlying, datetime.now(UTC))
        return next(
            (quote for quote in self._chain.quotes if quote.contract.contract_symbol == contract.contract_symbol),
            None,
        )


def option_mandate() -> Mandate:
    base = mandate()
    return base.model_copy(
        update={
            "options": base.options.model_copy(
                update={
                    "enabled": True,
                    "underlying_universe": ["NVDA"],
                    "max_loss_per_position_usd": 500.0,
                    "min_open_interest": 100,
                    "max_rel_spread_pct": 10.0,
                    "max_net_portfolio_delta_abs": 10_000.0,
                    "max_net_portfolio_vega_abs": 10_000.0,
                }
            )
        }
    )


def option_llm() -> object:
    llm = fake_llm()
    llm.responses["options_analyst"] = {
        "content": "IV is fair and calls are liquid.",
        "iv_regime": "fair",
        "expected_move_pct": 4.0,
        "skew_note": "Fixture skew is flat.",
        "event_risk": [],
        "confidence": 70,
    }
    llm.responses["trader"] = {
        "content": "Open the offered long call.",
        "action": "OPEN",
        "strategy": "long_call",
        "legs": [],
        "candidate_id": "candidate_1",
        "max_loss_usd": Decimal("1"),
        "time_horizon_days": 20,
        "entry_rationale": "Bullish plan with liquid calls.",
        "exit_plan": "Exit on thesis break.",
        "stop_loss_pct_premium": 50.0,
        "take_profit_pct_premium": 100.0,
    }
    return llm


async def test_full_option_decision_run_fills_and_journals(tmp_path: Path) -> None:
    llm = option_llm()
    active = OrchestratorRunner(
        bus=EventBus(),
        conn=conn(tmp_path),
        settings=settings(),
        mandate=option_mandate(),
        llm=llm,  # type: ignore[arg-type]
        router=OptionFixtureRouter(),  # type: ignore[arg-type]
    )
    queue = await active.bus.subscribe_queue()

    state = await active.run("NVDA", as_of=datetime(2026, 7, 7, 19, 30, tzinfo=UTC))

    assert state.status == "completed"
    assert state.option_chain is not None
    assert state.option_candidates
    assert state.option_proposal is not None
    assert state.final_order is not None
    assert state.final_order.asset_type == "option"
    assert active.conn.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0] == 1
    journal = active.conn.execute(
        "SELECT action, size, strategy, venue FROM journal WHERE run_id = ?",
        (state.run_id,),
    ).fetchone()
    assert journal["action"] == "OPEN_OPTION"
    assert journal["size"] == pytest.approx(float(state.option_proposal.max_loss_usd))
    assert journal["strategy"] == "long_call"
    assert journal["venue"] == "paper"
    entry = get_journal_entry(active.conn, state.run_id)
    assert entry is not None
    profitable_entry = entry.model_copy(update={"realized_ret": 0.25})
    assert _grade_option_entry(profitable_entry, bench_ret=0.0) == "good_call"
    assert (sentinel_home() / "runs" / state.run_id / "chain.parquet").exists()
    assert (sentinel_home() / "runs" / state.run_id / "candidates.json").exists()

    events = []
    while not queue.empty():
        events.append(await queue.get())
    completed_agents = [event.report.agent for event in events if isinstance(event, AgentCompleted)]
    interesting = [type(event) for event in events if isinstance(event, GateEvaluated | OrderFilled)]
    assert "options_analyst" in completed_agents
    assert interesting.index(GateEvaluated) < interesting.index(OrderFilled)


async def test_option_resume_uses_checkpointed_candidates(tmp_path: Path) -> None:
    llm = option_llm()
    router = OptionFixtureRouter()
    first = OrchestratorRunner(
        settings=settings(),
        mandate=option_mandate(),
        llm=llm,  # type: ignore[arg-type]
        router=router,  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )
    partial = await first.run("NVDA", as_of=datetime(2026, 7, 7, 19, 30, tzinfo=UTC), stop_after="research_manager")
    assert partial.option_chain is not None
    assert partial.option_candidates

    resume_router = OptionFixtureRouter()
    second = OrchestratorRunner(
        settings=settings(),
        mandate=option_mandate(),
        llm=llm,  # type: ignore[arg-type]
        router=resume_router,  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )
    resumed = await second.resume(partial.run_id)

    assert resumed.status == "completed"
    assert resumed.option_proposal is not None
    assert resumed.final_order is not None
    assert resume_router.chain_calls == 0


async def test_option_kill_switch_mid_run_halts(tmp_path: Path) -> None:
    active = OrchestratorRunner(
        settings=settings(),
        mandate=option_mandate(),
        llm=option_llm(),  # type: ignore[arg-type]
        router=OptionFixtureRouter(),  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )
    state = await active.run("NVDA", as_of=datetime(2026, 7, 7, 19, 30, tzinfo=UTC), stop_after="mandate_gate")
    assert state.final_order is not None

    engage(actor="test")
    resumed = await active.resume(state.run_id)

    assert resumed.status == "halted"
    assert active.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


async def test_default_equity_run_does_not_call_option_llm(tmp_path: Path) -> None:
    llm = fake_llm()
    active = OrchestratorRunner(
        settings=settings(),
        mandate=mandate(),
        llm=llm,
        router=OptionFixtureRouter(),  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )

    state = await active.run("NVDA", as_of=datetime(2026, 7, 7, 19, 30, tzinfo=UTC))

    assert state.status == "completed"
    assert state.option_chain is None
    assert state.option_candidates == []
    assert state.final_order is not None
    assert state.final_order.asset_type == "equity"
    assert "options_analyst" not in [call.agent for call in llm.calls]


async def test_option_run_fake_llm_cost_stays_under_budget(tmp_path: Path) -> None:
    active = OrchestratorRunner(
        settings=settings(),
        mandate=option_mandate(),
        llm=option_llm(),  # type: ignore[arg-type]
        router=OptionFixtureRouter(),  # type: ignore[arg-type]
        db_path=tmp_path / "sentinel.db",
    )

    state = await active.run("NVDA", as_of=datetime(2026, 7, 7, 19, 30, tzinfo=UTC))

    assert state.total_cost_usd < Decimal("0.50")


async def test_fetch_data_enriches_live_router_chain_and_candidates_use_iv_rank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sentinel.data.router.risk_free_rate",
        lambda *args, **kwargs: RateResult(0.04, "fixture"),
    )
    active_conn = conn(tmp_path)
    active_conn.execute(
        "INSERT INTO iv_history (symbol, date, atm_iv, rv_yz) VALUES (?, ?, ?, ?)",
        ("NVDA", "2026-07-01", 0.10, 0.15),
    )
    active_conn.execute(
        "INSERT INTO iv_history (symbol, date, atm_iv, rv_yz) VALUES (?, ?, ?, ?)",
        ("NVDA", "2026-07-02", 0.20, 0.16),
    )
    active_conn.commit()
    router = DataRouter(loaders=[LiveMarketFake(), LiveOptionsFake()])
    active = OrchestratorRunner(
        bus=EventBus(),
        conn=active_conn,
        settings=settings(),
        mandate=option_mandate(),
        llm=option_llm(),  # type: ignore[arg-type]
        router=router,
    )

    state = await active.run("NVDA", as_of=datetime(2026, 7, 7, 19, 30, tzinfo=UTC), stop_after="research_manager")

    assert state.option_chain is not None
    assert state.option_chain.atm_iv == pytest.approx(0.25)
    assert state.option_chain.rv_yang_zhang is not None
    assert state.option_chain.iv_rank is not None
    assert state.option_chain.iv_rank > 0.5
    saved = active_conn.execute(
        "SELECT atm_iv, rv_yz FROM iv_history WHERE symbol = ? AND date = ?",
        ("NVDA", "2026-07-07"),
    ).fetchone()
    assert saved is not None
    assert saved["atm_iv"] == pytest.approx(0.25)
    assert saved["rv_yz"] is not None
    assert state.option_candidates
    assert "iv_rank=1.50" in state.option_candidates[0].rationale_facts


class LiveMarketFake(FixtureRouter):
    name = "yfinance"

    def get_quote(self, symbol: str):
        return super().get_quote(symbol)


class LiveOptionsFake:
    name = "yfinance_options"

    def load_option_chain(
        self,
        underlying: str,
        as_of: datetime,
        spot: float,
        risk_free_rate: float,
        dividend_yield: float,
        *,
        min_dte: int,
        max_dte: int,
    ) -> LoadedOptionChain:
        _ = (spot, risk_free_rate, dividend_yield, min_dte, max_dte)
        chain = _option_chain(underlying, as_of).model_copy(
            update={"atm_iv": None, "iv_rank": None, "iv_percentile": None, "rv_yang_zhang": None}
        )
        return LoadedOptionChain(expiries=chain.expiries, quotes=chain.quotes)


def _option_chain(underlying: str, as_of: datetime) -> OptionChainSnapshot:
    expiry = as_of.date() + timedelta(days=45)
    quotes = [
        _quote(underlying, "call", Decimal("100"), expiry, Decimal("1.90"), Decimal("2.00"), 0.60, as_of),
        _quote(underlying, "call", Decimal("105"), expiry, Decimal("0.95"), Decimal("1.00"), 0.30, as_of),
        _quote(underlying, "put", Decimal("100"), expiry, Decimal("1.85"), Decimal("1.95"), -0.40, as_of),
        _quote(underlying, "put", Decimal("95"), expiry, Decimal("0.90"), Decimal("0.95"), -0.30, as_of),
    ]
    return OptionChainSnapshot(
        run_id="fixture",
        underlying=underlying.upper(),
        as_of=as_of,
        spot=Decimal("100"),
        risk_free_rate=0.04,
        dividend_yield=0.0,
        expiries=[expiry],
        quotes=quotes,
        atm_iv=0.25,
        iv_rank=0.30,
        iv_percentile=0.40,
        rv_yang_zhang=0.20,
        pricing_source="live_chain",
        providers_used={"options": "fixture"},
    )


def _quote(
    underlying: str,
    kind: str,
    strike: Decimal,
    expiry,
    bid: Decimal,
    ask: Decimal,
    delta: float,
    ts: datetime,
) -> OptionQuote:
    contract = OptionContract(
        contract_symbol=f"{underlying.upper()}{expiry:%y%m%d}{kind[0].upper()}{int(strike):08d}",
        underlying=underlying.upper(),
        kind=kind,  # type: ignore[arg-type]
        strike=strike,
        expiry=expiry,
    )
    return OptionQuote(
        contract=contract,
        bid=bid,
        ask=ask,
        last=(bid + ask) / Decimal("2"),
        volume=1_000,
        open_interest=500,
        implied_vol=0.25,
        ts=ts,
        source="fixture",
        model_iv=0.25,
        delta=delta,
        gamma=0.01,
        vega=0.10,
        theta=-0.01,
    )

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from sentinel.core.models import PMDecision, TradeProposal
from sentinel.eval.metrics import (
    RESULT_COLUMNS,
    build_result_row,
    decision_costs,
    format_results_table,
    results_to_csv,
    row_from_record,
    trade_frequency,
)
from sentinel.eval.sentinel_adapter import decision_to_signal
from sentinel.llm.cost import record_cost
from sentinel.orchestrator.state import initial_state
from sentinel.store.db import connect, run_migrations
from tests.orchestrator._helpers import mandate

_ENVELOPE = {
    "run_id": "r", "model": "m", "created_at": datetime(2026, 1, 1, tzinfo=UTC), "latency_ms": 1,
    "input_tokens": 0, "output_tokens": 0, "cost_usd": Decimal("0"), "content": "c",
}


def _proposal(action: str, qty: float = 50.0) -> TradeProposal:
    return TradeProposal(
        **_ENVELOPE, agent="trader", action=action, quantity_pct=qty, order_type="market",
        time_horizon_days=5, entry_rationale="x", exit_plan="y", stop_loss_pct=None, take_profit_pct=None,
    )


def _pm(verdict: str, approved: float = 50.0) -> PMDecision:
    return PMDecision(**_ENVELOPE, agent="portfolio_manager", verdict=verdict, approved_quantity_pct=approved, reasoning="r", lessons_applied=[])


def test_decision_to_signal_sizes_like_the_mandate_gate() -> None:
    state = initial_state("NVDA")
    state.trade_proposal = _proposal("BUY", 50.0)
    state.pm_decision = _pm("APPROVE", 50.0)
    m = mandate()  # max_position_pct_equity = 10

    signal = decision_to_signal(state, mandate=m, equity=10_000.0)

    assert signal.action == "BUY"
    # 10% allowance x 50% requested x (50/50 PM scale) = 5% of equity
    assert signal.size is not None and abs(signal.size - 0.05) < 1e-9


def test_decision_to_signal_hold_paths() -> None:
    m = mandate()
    hold = initial_state("NVDA")
    hold.trade_proposal = _proposal("HOLD", 0.0)
    hold.pm_decision = _pm("APPROVE", 0.0)
    assert decision_to_signal(hold, mandate=m, equity=1.0).action == "HOLD"

    rejected = initial_state("NVDA")
    rejected.trade_proposal = _proposal("BUY")
    rejected.pm_decision = _pm("REJECT", 0.0)
    assert decision_to_signal(rejected, mandate=m, equity=1.0).action == "HOLD"

    failed = initial_state("NVDA")
    failed.status = "failed"
    failed.trade_proposal = _proposal("BUY")
    failed.pm_decision = _pm("APPROVE")
    assert decision_to_signal(failed, mandate=m, equity=1.0).action == "HOLD"

    sell = initial_state("NVDA")
    sell.trade_proposal = _proposal("SELL")
    sell.pm_decision = _pm("APPROVE")
    assert decision_to_signal(sell, mandate=m, equity=1.0).model_dump() == {"action": "SELL", "size": 1.0}

    revised = initial_state("NVDA")
    revised.trade_proposal = _proposal("BUY", 100.0)
    revised.pm_decision = _pm("REVISE", 25.0)
    assert abs(decision_to_signal(revised, mandate=m, equity=1.0).size - 0.025) < 1e-9


def test_decision_costs_aggregate_per_bt_id_from_sqlite(tmp_path: Path) -> None:
    conn = connect(tmp_path / "e.db")
    run_migrations(conn)
    for run_id, agent, tokens, latency in (
        ("exp__p__NVDA:2025-01-02", "market_analyst", (100, 20), 300),
        ("exp__p__NVDA:2025-01-02", "trader", (200, 40), 500),
        ("exp__p__NVDA:2025-01-09", "trader", (150, 30), 400),
        ("exp__other__NVDA:2025-01-02", "trader", (999, 999), 999),  # different pipeline, ignored
    ):
        record_cost(conn, run_id=run_id, agent=agent, model="llama", tokens_in=tokens[0], tokens_out=tokens[1], latency_ms=latency)

    summary = decision_costs(conn, "exp__p__NVDA")

    assert summary.decisions_with_costs == 2
    assert summary.total_tokens == 540
    assert summary.total_cost_usd == 0.0
    assert summary.total_llm_latency_ms == 1200
    assert summary.per_decision(2) == (270.0, 0.0, 600.0)
    assert summary.per_decision(0) == (0.0, 0.0, 0.0)


def test_trade_frequency_is_annualized_per_bar() -> None:
    assert trade_frequency(10, 252) == 10.0
    assert trade_frequency(3, 126) == 6.0
    assert trade_frequency(3, 0) == 0.0


class _Result:
    trades: ClassVar[list[dict[str, float]]] = [{"pnl": 1.0}, {"pnl": -1.0}]
    total_return = 0.1
    annualized_return = 0.2
    sharpe = float("nan")  # must be sanitised to 0.0
    max_drawdown = -0.05
    win_rate = 0.5
    exposure_pct = 0.8
    benchmark_return = 0.12
    equity_curve: ClassVar[list[dict[str, object]]] = [{"date": "2025-01-02", "equity": 10_000.0}]


def test_result_row_roundtrips_through_csv_and_record(tmp_path: Path) -> None:
    from sentinel.eval.metrics import DecisionCostSummary

    row = build_result_row(
        experiment="e", pipeline="p", config={"debate_enabled": True}, symbol="NVDA",
        start="2025-01-02", end="2025-03-01", bt_id="e__p__NVDA", result=_Result(), bars=40,
        decision_summary={"decisions": 8, "signals": 4, "failed_decisions": 1, "avg_wall_ms": 12.5},
        costs=DecisionCostSummary(8, 800, 0.0, 4000), notes=["2025-02-01: failed boom"],
    )
    assert row.sharpe == 0.0 and row.trade_frequency == 4 / 40 * 252
    assert row.avg_tokens_per_decision == 100 and row.avg_llm_latency_ms_per_decision == 500
    assert list(row.as_record()) == list(RESULT_COLUMNS)

    path = results_to_csv([row], tmp_path / "r.csv")
    import csv

    with path.open() as handle:
        record = next(csv.DictReader(handle))
    rebuilt = row_from_record(record)
    assert rebuilt.as_record() == row.as_record()

    text = format_results_table([row])
    assert "Experiment e" in text and "failed boom" in text and "50.00%" in text

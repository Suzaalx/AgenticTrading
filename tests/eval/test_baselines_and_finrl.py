from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from sentinel.backtest.strategies.base import BarContext
from sentinel.eval import ExperimentSpec, run_experiment_async
from sentinel.eval.finrl_adapter import (
    ACTION_BUY,
    ACTION_HOLD,
    ACTION_SELL,
    OBSERVATION_FEATURES,
    CallablePolicy,
    FinRLStrategy,
    StubPolicy,
    TablePolicy,
    build_observation,
    load_policy,
    train_policy,
)
from tests.eval.test_experiment import synthetic_bars
from tests.orchestrator._helpers import FixtureRouter, fake_llm, mandate, settings


def _context(bars: pd.DataFrame, upto: int, *, position_qty: float = 0.0) -> BarContext:
    history = bars.iloc[:upto]
    close = float(history["close"].iloc[-1])
    return BarContext(
        symbol="NVDA",
        timestamp=history.index[-1],
        bar=history.iloc[-1],
        history=history,
        cash=10_000.0 - position_qty * close,
        position_qty=position_qty,
        position_value=position_qty * close,
        equity=10_000.0,
    )


def test_observation_has_the_shared_feature_layout() -> None:
    bars = synthetic_bars()
    obs = build_observation(_context(bars, 60, position_qty=10.0))

    assert obs.names == OBSERVATION_FEATURES
    assert len(obs.values) == len(OBSERVATION_FEATURES)
    assert all(isinstance(v, float) for v in obs.values)
    assert 0 < dict(zip(obs.names, obs.values, strict=True))["position_frac"] < 1


def test_load_policy_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINRL_POLICY_PATH", raising=False)
    assert isinstance(load_policy(None), StubPolicy)
    assert isinstance(load_policy(tmp_path / "missing.json"), StubPolicy)
    assert isinstance(load_policy(tmp_path / "model.zip"), StubPolicy) or not (tmp_path / "model.zip").exists()

    export = tmp_path / "policy.json"
    export.write_text(json.dumps({"name": "ppo_export", "buy_if": {"sma_ratio_20": 0.0}, "sell_if": {"sma_ratio_20": -0.02}}))
    table = load_policy(export)
    assert isinstance(table, TablePolicy) and table.name == "ppo_export"

    monkeypatch.setenv("FINRL_POLICY_PATH", str(export))
    assert isinstance(load_policy(None), TablePolicy)

    fn = load_policy(lambda values: ACTION_BUY)
    assert isinstance(fn, CallablePolicy) and fn.act(build_observation(_context(synthetic_bars(), 60))) == ACTION_BUY


def test_finrl_strategy_maps_actions_to_engine_signals() -> None:
    bars = synthetic_bars()
    always_buy = FinRLStrategy(policy=CallablePolicy(fn=lambda _: ACTION_BUY, name="buy"), warmup_bars=10)
    assert always_buy.on_bar(_context(bars, 5)).action == "HOLD"  # warm-up
    assert always_buy.on_bar(_context(bars, 30)).action == "BUY"
    assert always_buy.on_bar(_context(bars, 31, position_qty=5)).action == "HOLD"  # already long

    seller = FinRLStrategy(policy=CallablePolicy(fn=lambda _: ACTION_SELL, name="sell"), warmup_bars=10)
    assert seller.on_bar(_context(bars, 30)).action == "HOLD"  # nothing to sell
    assert seller.on_bar(_context(bars, 30, position_qty=5)).model_dump() == {"action": "SELL", "size": 1.0}

    stub = FinRLStrategy(policy=StubPolicy(), warmup_bars=1)
    assert stub.on_bar(_context(bars, 30)).action == "HOLD" and stub.is_stub
    assert stub.notes()[0].startswith("STUB:")
    assert stub.policy.act(build_observation(_context(bars, 30))) == ACTION_HOLD


def test_train_policy_is_an_explicit_stub() -> None:
    with pytest.raises(NotImplementedError, match="not wired this cycle"):
        train_policy(synthetic_bars(), out_path=Path("x"))


@pytest.mark.asyncio
async def test_baselines_and_finrl_share_the_results_table_with_sentinel(tmp_path: Path) -> None:
    bars = synthetic_bars()
    export = tmp_path / "policy.json"
    export.write_text(json.dumps({"name": "toy_table", "buy_if": {"sma_ratio_20": 0.01}, "sell_if": {"sma_ratio_20": -0.01}}))
    spec = ExperimentSpec(
        experiment_id="baselines",
        symbols=["NVDA"],
        start=bars.index[0].date(),
        end=bars.index[-1].date(),
        pipelines=["sentinel_debate_off", "sma_cross", "buy_hold", "finrl"],
        starting_cash=10_000.0,
        strategy_params={"short_window": 5, "long_window": 20, "finrl_policy": export, "finrl_warmup_bars": 20},
        out_dir=tmp_path / "exp",
    )
    llm = fake_llm()
    for name in ("bull_researcher", "bear_researcher", "debate_convergence_classifier", "research_manager"):
        llm.responses.pop(name)

    rows = await run_experiment_async(
        spec, settings=settings(), mandate=mandate(), llm=llm, router=FixtureRouter(), bars={"NVDA": bars}
    )

    by = {r.pipeline: r for r in rows}
    assert set(by) == {"sentinel_debate_off", "sma_cross", "buy_hold", "finrl"}
    # Comparable: same symbol/window/bars, same column set, all rule rows cost nothing.
    assert {(r.symbol, r.start, r.end, r.bars) for r in rows} == {("NVDA", spec.start.isoformat(), spec.end.isoformat(), len(bars))}
    assert {tuple(r.as_record()) for r in rows} == {tuple(rows[0].as_record())}
    for name in ("sma_cross", "buy_hold", "finrl"):
        assert by[name].total_tokens == 0 and by[name].avg_cost_usd_per_decision == 0.0
        assert by[name].decisions == len(bars)
    assert by["sma_cross"].round_trips >= 1 and by["finrl"].signals >= 1
    assert json.loads(by["finrl"].config) == {
        "cadence": "daily",
        "policy": "toy_table",
        "strategy": "finrl",
        "strategy_params": {"finrl_policy": str(export), "finrl_warmup_bars": 20, "long_window": 20, "short_window": 5},
        "stub": False,
    }
    assert by["finrl"].notes == "policy=toy_table"


@pytest.mark.asyncio
async def test_finrl_stub_row_is_flagged_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINRL_POLICY_PATH", raising=False)
    bars = synthetic_bars()
    spec = ExperimentSpec(
        experiment_id="stub",
        symbols=["NVDA"],
        start=bars.index[0].date(),
        end=bars.index[-1].date(),
        pipelines=["buy_hold", "finrl"],
        out_dir=tmp_path / "exp",
    )

    rows = await run_experiment_async(spec, settings=settings(), mandate=mandate(), llm=fake_llm(), bars={"NVDA": bars})

    finrl = next(r for r in rows if r.pipeline == "finrl")
    assert finrl.signals == 0 and finrl.total_return == 0.0
    assert json.loads(finrl.config)["stub"] is True
    assert finrl.notes.startswith("STUB: no trained FinRL policy")

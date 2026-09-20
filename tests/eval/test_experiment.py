from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sentinel.eval import ExperimentSpec, load_result_rows, run_experiment_async
from sentinel.eval.metrics import RESULT_COLUMNS
from sentinel.store.db import connect, run_migrations
from tests.orchestrator._helpers import FixtureRouter, fake_llm, mandate, settings


def synthetic_bars(n: int = 120, seed: int = 7) -> pd.DataFrame:
    """Deterministic trending bars with a dip so SMA crossover actually trades."""

    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2025-01-02", periods=n, name="date")
    cycles = 8.0 * np.sin(np.linspace(0, 3 * np.pi, n))  # ~3 regime flips -> several SMA crosses
    close = 100 + np.linspace(0, 6, n) + cycles + rng.normal(0, 0.4, n).cumsum() * 0.2
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000_000,
        },
        index=index,
    )


def _spec(tmp_path: Path, pipelines: list[str]) -> ExperimentSpec:
    bars = synthetic_bars()
    return ExperimentSpec(
        experiment_id="exp_test",
        symbols=["NVDA"],
        start=bars.index[0].date(),
        end=bars.index[-1].date(),
        pipelines=pipelines,
        cadence="weekly",
        starting_cash=10_000.0,
        strategy_params={"short_window": 5, "long_window": 20},
        out_dir=tmp_path / "exp",
    )


def _llm_without_debate_roles():
    llm = fake_llm()
    for name in ("bull_researcher", "bear_researcher", "debate_convergence_classifier", "research_manager"):
        llm.responses.pop(name)
    return llm


@pytest.mark.asyncio
async def test_experiment_runs_all_pipelines_on_identical_bars_and_writes_table(tmp_path: Path) -> None:
    spec = _spec(tmp_path, ["sentinel_debate_on", "sentinel_debate_off", "sma_cross", "buy_hold"])
    bars = synthetic_bars()
    llm = fake_llm()

    rows = await run_experiment_async(
        spec,
        settings=settings(),
        mandate=mandate(),
        llm=llm,
        router=FixtureRouter(),
        bars={"NVDA": bars},
    )

    assert [row.pipeline for row in rows] == ["sentinel_debate_on", "sentinel_debate_off", "sma_cross", "buy_hold"]
    by_name = {row.pipeline: row for row in rows}
    # Same window, same bars for everyone.
    assert {row.bars for row in rows} == {len(bars)}
    assert {(row.start, row.end) for row in rows} == {(spec.start.isoformat(), spec.end.isoformat())}
    # Rule strategies decide every bar; agent pipelines decide on the weekly cadence.
    assert by_name["sma_cross"].decisions == len(bars)
    assert by_name["buy_hold"].decisions == len(bars)
    assert by_name["sentinel_debate_on"].decisions == len(range(0, len(bars), 5))
    # Buy-and-hold: one signal, fully invested, never closes -> no round trips, win rate n/a (0).
    assert by_name["buy_hold"].signals == 1 and by_name["buy_hold"].round_trips == 0
    assert by_name["buy_hold"].total_return == pytest.approx(
        bars["close"].iloc[-1] / (bars["open"].iloc[1] * 1.0005) - 1, rel=0.02
    )
    assert by_name["sma_cross"].round_trips >= 1
    # Sentinel fixture always BUYs 50% of the mandate allowance -> sized 5% of equity per decision.
    on = by_name["sentinel_debate_on"]
    assert on.signals == on.decisions
    assert 0 < on.exposure_pct <= 1
    assert on.total_cost_usd == 0.0  # FakeLLM meters zero dollars but every call is logged
    # Debate off uses fewer LLM calls per decision than debate on (no bull/bear/judge).
    on_calls = sum(1 for c in llm.calls if c.agent in {"bull_researcher", "bear_researcher", "research_manager"})
    assert on_calls > 0
    off_config = json.loads(by_name["sentinel_debate_off"].config)
    assert off_config["debate_enabled"] is False and off_config["cadence"] == "weekly"
    assert set(off_config["llm"]) == {
        "provider", "quick_model", "deep_model", "role_models", "analyst_history_bars", "max_debate_rounds",
        "max_position_pct_equity",
    }
    assert off_config["llm"]["max_position_pct_equity"] == 10.0
    # Cost metering is derived from the experiment DB, keyed per bt_id.
    conn = connect(spec.db_path())
    run_migrations(conn)
    metered = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM costs WHERE run_id LIKE ?", (f"{on.bt_id}:%",)
    ).fetchone()[0]
    assert metered == on.decisions
    assert conn.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM backtests").fetchone()[0] == 4
    # CSV is tidy: one row per pipeline with the stable column set.
    with spec.csv_path().open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(RESULT_COLUMNS)
        csv_rows = list(reader)
    assert len(csv_rows) == 4
    assert {r["pipeline"] for r in csv_rows} == set(by_name)
    conn.close()


@pytest.mark.asyncio
async def test_results_are_reproducible_from_stored_runs_and_reruns_replace(tmp_path: Path) -> None:
    spec = _spec(tmp_path, ["sentinel_debate_off", "buy_hold"])
    bars = synthetic_bars()
    kwargs = dict(settings=settings(), mandate=mandate(), llm=_llm_without_debate_roles(), router=FixtureRouter(), bars={"NVDA": bars})

    first = await run_experiment_async(spec, **kwargs)
    second = await run_experiment_async(spec, **kwargs)  # re-run same experiment id

    conn = connect(spec.db_path())
    run_migrations(conn)
    stored = load_result_rows(conn, "exp_test")
    assert [r.pipeline for r in stored] == ["buy_hold", "sentinel_debate_off"]  # ordered by symbol, pipeline
    stored_by = {r.pipeline: r for r in stored}
    for row in second:
        assert stored_by[row.pipeline].as_record() == row.as_record()
    # Everything except wall-clock timings is bit-for-bit reproducible.
    timing = {"avg_llm_latency_ms_per_decision", "avg_wall_ms_per_decision"}
    strip = lambda r: {k: v for k, v in r.as_record().items() if k not in timing}  # noqa: E731
    assert [strip(r) for r in first] == [strip(r) for r in second]
    # Costs were not double-counted by the re-run.
    off = stored_by["sentinel_debate_off"]
    metered = conn.execute(
        "SELECT COUNT(*) FROM costs WHERE run_id LIKE ?", (f"{off.bt_id}:%",)
    ).fetchone()[0]
    assert metered == off.decisions * 8  # 4 analysts + trader + 3 risk debaters... per decision
    conn.close()


def test_unknown_pipeline_is_rejected(tmp_path: Path) -> None:
    from sentinel.eval.pipelines import get_pipeline

    with pytest.raises(ValueError, match="unknown pipeline"):
        get_pipeline("nope")


@pytest.mark.asyncio
async def test_max_position_pct_override_changes_sentinel_exposure(tmp_path: Path) -> None:
    bars = synthetic_bars()
    base = _spec(tmp_path, ["sentinel_debate_off"])
    full = ExperimentSpec(**{**base.__dict__, "experiment_id": "full", "max_position_pct_equity": 100.0, "out_dir": tmp_path / "full"})
    kwargs = dict(settings=settings(), mandate=mandate(), router=FixtureRouter(), bars={"NVDA": bars})

    capped = (await run_experiment_async(base, llm=_llm_without_debate_roles(), **kwargs))[0]
    uncapped = (await run_experiment_async(full, llm=_llm_without_debate_roles(), **kwargs))[0]

    assert json.loads(capped.config)["llm"]["max_position_pct_equity"] == 10.0
    assert json.loads(uncapped.config)["llm"]["max_position_pct_equity"] == 100.0
    # Same decisions, bigger fills: fixture BUYs 50% of the allowance each week.
    assert uncapped.signals == capped.signals
    assert abs(uncapped.total_return) > abs(capped.total_return)

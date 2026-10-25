from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sentinel.eval import ExperimentSpec, run_experiment_async
from sentinel.eval.tradingagents_adapter import RATING_ACTIONS, _CostMeter, rating_to_signal
from sentinel.store.db import connect, run_migrations
from tests.eval.test_experiment import synthetic_bars
from tests.orchestrator._helpers import FixtureRouter, fake_llm, mandate, settings


class FakeTAGraph:
    """Stands in for TradingAgentsGraph: scripted ratings, and it reports token usage
    through the callbacks it was given, the way LangChain does."""

    def __init__(self, callbacks: list[Any], ratings: list[str]) -> None:
        self.callbacks = callbacks
        self.ratings = list(ratings)
        self.dates: list[str] = []

    def propagate(self, ticker: str, trade_date: str):
        self.dates.append(trade_date)
        for handler in self.callbacks:  # two LLM calls per decision
            for node, tin in (("Market Analyst", 900), ("Portfolio Manager", 1500)):
                message = SimpleNamespace(usage_metadata={"input_tokens": tin, "output_tokens": 100},
                                          response_metadata={"model_name": "gemini-test"})
                response = SimpleNamespace(generations=[[SimpleNamespace(message=message)]])
                handler.on_llm_start({}, ["p"], run_id=(trade_date, node))
                handler.on_llm_end(response, run_id=(trade_date, node), metadata={"langgraph_node": node})
        rating = self.ratings.pop(0) if self.ratings else "Hold"
        if rating == "BOOM":
            raise RuntimeError("provider exploded")
        return {"final_trade_decision": f"Rating: {rating}"}, rating


def test_rating_mapping_uses_the_mandate_allowance() -> None:
    m = mandate()  # max_position_pct_equity = 10
    assert rating_to_signal("Buy", mandate=m).model_dump() == {"action": "BUY", "size": pytest.approx(0.10)}
    assert rating_to_signal("Overweight", mandate=m).size == pytest.approx(0.05)
    assert rating_to_signal("Hold", mandate=m).action == "HOLD"
    assert rating_to_signal("Underweight", mandate=m).model_dump() == {"action": "SELL", "size": 0.5}
    assert rating_to_signal("Sell", mandate=m).model_dump() == {"action": "SELL", "size": 1.0}
    assert rating_to_signal("REVIEW", mandate=m).action == "HOLD"  # unreadable decision never trades
    assert set(RATING_ACTIONS) == {"buy", "overweight", "hold", "underweight", "sell"}


def test_cost_meter_writes_sentinel_cost_rows(tmp_path: Path) -> None:
    conn = connect(tmp_path / "c.db")
    run_migrations(conn)
    meter = _CostMeter(conn)
    meter.run_id = "exp__tradingagents__NVDA:2025-03-03"
    FakeTAGraph([meter.make_handler()], ["Buy"]).propagate("NVDA", "2025-03-03")
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0  # buffered until flush
    assert meter.flush() == 2

    rows = conn.execute("SELECT agent, model, tokens_in, tokens_out, attempts FROM costs").fetchall()
    assert [(r["agent"], r["tokens_in"]) for r in rows] == [("ta:Market Analyst", 900), ("ta:Portfolio Manager", 1500)]
    assert {r["model"] for r in rows} == {"gemini-test"} and {r["attempts"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_tradingagents_runs_through_the_shared_harness(tmp_path: Path) -> None:
    bars = synthetic_bars()
    graphs: list[FakeTAGraph] = []

    def factory(callbacks: list[Any]) -> FakeTAGraph:
        graphs.append(FakeTAGraph(callbacks, ["Buy", "Hold", "BOOM", "Sell", "Overweight"]))
        return graphs[-1]

    spec = ExperimentSpec(
        experiment_id="ta", symbols=["NVDA"], start=bars.index[0].date(), end=bars.index[-1].date(),
        pipelines=["tradingagents", "buy_hold"], cadence="weekly",
        strategy_params={"ta_graph_factory": factory}, out_dir=tmp_path / "exp",
    )
    rows = await run_experiment_async(spec, settings=settings(), mandate=mandate(), llm=fake_llm(),
                                      router=FixtureRouter(), bars={"NVDA": bars})

    ta = next(r for r in rows if r.pipeline == "tradingagents")
    weeks = len(range(0, len(bars), 5))
    assert ta.decisions == weeks and len(graphs) == 1  # one graph reused for the whole window
    assert graphs[0].dates[0] == bars.index[0].date().isoformat()  # decides on the snapshot's own date
    assert ta.failed_decisions == 1 and "provider exploded" in ta.notes
    assert ta.signals == 3  # Buy, Sell, Overweight
    assert ta.avg_tokens_per_decision == pytest.approx((900 + 1500 + 200) * (weeks) / ta.decisions)
    assert ta.first_pass_valid_rate == 1.0
    cfg = json.loads(ta.config)
    assert cfg["system"] == "tradingagents" and cfg["llm"]["provider"] == settings().llm.provider
    conn = connect(spec.db_path())
    run_migrations(conn)
    assert conn.execute("SELECT COUNT(DISTINCT run_id) FROM costs WHERE agent LIKE 'ta:%'").fetchone()[0] == weeks


def test_config_maps_providers_and_isolates_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tradingagents")
    from sentinel.eval.tradingagents_adapter import tradingagents_config

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    cfg = tradingagents_config(provider="gemini", quick_model="gemini-2.5-flash", deep_model="gemini-2.5-flash",
                               workdir=tmp_path / "ta")
    import os

    assert cfg["llm_provider"] == "google" and os.environ["GOOGLE_API_KEY"] == "g-test"
    assert cfg["memory_log_path"].startswith(str(tmp_path)) and cfg["results_dir"].startswith(str(tmp_path))
    assert cfg["checkpoint_enabled"] is False
    with pytest.raises(ValueError, match="no mapping"):
        tradingagents_config(provider="mystery", quick_model="m", deep_model="m", workdir=tmp_path)

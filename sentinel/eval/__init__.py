"""Shared evaluation harness: run several trading pipelines on identical data and compare.

Public surface:

- :func:`sentinel.eval.experiment.run_experiment` — run an :class:`ExperimentSpec`
  (fixed symbols, fixed window, chosen pipelines) and return tidy result rows.
- :mod:`sentinel.eval.pipelines` — the registry of comparable pipelines
  (``sentinel_debate_on``, ``sentinel_debate_off``, rule baselines, ...).
- :mod:`sentinel.eval.metrics` — per-row metrics (return, Sharpe, drawdown, win rate,
  trade frequency, cost & latency per decision) plus CSV / console rendering.
"""

from sentinel.eval.experiment import (
    ExperimentSpec,
    load_result_rows,
    run_experiment,
    run_experiment_async,
)
from sentinel.eval.metrics import ResultRow, render_results_table, results_to_csv
from sentinel.eval.pipelines import PIPELINES, PipelineSpec, get_pipeline

__all__ = [
    "PIPELINES",
    "ExperimentSpec",
    "PipelineSpec",
    "ResultRow",
    "get_pipeline",
    "load_result_rows",
    "render_results_table",
    "results_to_csv",
    "run_experiment",
    "run_experiment_async",
]

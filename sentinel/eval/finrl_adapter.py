"""FinRL integration seam for the evaluation harness.

FinRL (https://github.com/AI4Finance-Foundation/FinRL) trains deep-RL agents on a
gym-style trading environment. Rather than importing FinRL into the harness directly
(heavy torch/gymnasium dependency, not installed in this repo), we define a small,
dependency-free interface that a FinRL-trained policy can be wrapped in, and a
:class:`FinRLStrategy` that drives that policy through the *same* backtest engine, bars,
fill model and metrics as every other pipeline.

Interface
---------
- :class:`Observation` — the feature vector the policy sees on each bar, built here from
  the bar history so the definition is shared by training and evaluation.
- :class:`FinRLPolicy` — ``act(observation) -> action`` where ``action`` is one of
  ``HOLD/BUY/SELL`` ids. Anything exposing that method (an SB3 ``model.predict``
  wrapper, a torch module, a lookup table) plugs in.
- :func:`load_policy` — resolves a policy from ``FINRL_POLICY_PATH`` / a callable; when
  nothing is available it returns :class:`StubPolicy` which always HOLDs and is flagged
  in the results row, so the interface is exercised end-to-end without a trained model.
- :func:`train_policy` — the training hook; raises ``NotImplementedError`` with the
  recipe this cycle (see the docstring), keeping the contract explicit.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import pandas as pd

from sentinel.backtest.strategies.base import BarContext
from sentinel.core.models import Signal

Action = Literal[0, 1, 2]
ACTION_HOLD: Action = 0
ACTION_BUY: Action = 1
ACTION_SELL: Action = 2
ACTION_NAMES = {ACTION_HOLD: "HOLD", ACTION_BUY: "BUY", ACTION_SELL: "SELL"}
POLICY_PATH_ENV = "FINRL_POLICY_PATH"
OBSERVATION_FEATURES: tuple[str, ...] = (
    "ret_1",  # 1-bar close-to-close return
    "ret_5",  # 5-bar return
    "ret_20",  # 20-bar return
    "sma_ratio_20",  # close / sma_20 - 1
    "sma_ratio_50",  # close / sma_50 - 1
    "vol_20",  # 20-bar return std
    "position_frac",  # position value / equity
)


@dataclass(frozen=True)
class Observation:
    """Feature vector shared by FinRL training and harness evaluation."""

    values: tuple[float, ...]
    names: tuple[str, ...] = OBSERVATION_FEATURES

    def as_list(self) -> list[float]:
        return list(self.values)


class FinRLPolicy(Protocol):
    """Minimal policy contract: an observation in, a discrete action id out."""

    name: str

    def act(self, observation: Observation) -> int: ...


@dataclass
class StubPolicy:
    """Placeholder used when no trained policy is available: always HOLD."""

    name: str = "finrl_stub_hold"
    reason: str = "no trained FinRL policy available (set FINRL_POLICY_PATH or pass policy=)"

    def act(self, observation: Observation) -> int:
        _ = observation
        return ACTION_HOLD


@dataclass
class CallablePolicy:
    """Adapt any ``f(observation_values) -> action`` (e.g. an SB3 ``predict`` wrapper)."""

    fn: Callable[[list[float]], int]
    name: str = "finrl_callable"

    def act(self, observation: Observation) -> int:
        return int(self.fn(observation.as_list()))


@dataclass
class TablePolicy:
    """Deterministic threshold policy loadable from JSON (what a tiny trained agent exports).

    JSON shape: ``{"name": "...", "buy_if": {"sma_ratio_20": 0.01}, "sell_if": {"sma_ratio_20": -0.01}}``
    -> BUY when every ``buy_if`` feature exceeds its threshold, SELL when every ``sell_if``
    feature is below its threshold, else HOLD.
    """

    buy_if: dict[str, float] = field(default_factory=dict)
    sell_if: dict[str, float] = field(default_factory=dict)
    name: str = "finrl_table"

    def act(self, observation: Observation) -> int:
        features = dict(zip(observation.names, observation.values, strict=True))
        if self.buy_if and all(features.get(k, 0.0) > v for k, v in self.buy_if.items()):
            return ACTION_BUY
        if self.sell_if and all(features.get(k, 0.0) < v for k, v in self.sell_if.items()):
            return ACTION_SELL
        return ACTION_HOLD

    @classmethod
    def from_json(cls, path: Path) -> TablePolicy:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            buy_if={k: float(v) for k, v in data.get("buy_if", {}).items()},
            sell_if={k: float(v) for k, v in data.get("sell_if", {}).items()},
            name=str(data.get("name", f"finrl_table:{path.name}")),
        )


def build_observation(context: BarContext) -> Observation:
    """Compute the shared feature vector from the bar history available at ``context``."""

    close = pd.to_numeric(context.history["close"], errors="coerce").astype(float)
    values = (
        _pct_change(close, 1),
        _pct_change(close, 5),
        _pct_change(close, 20),
        _sma_ratio(close, 20),
        _sma_ratio(close, 50),
        _rolling_vol(close, 20),
        (context.position_value / context.equity) if context.equity > 0 else 0.0,
    )
    return Observation(values=tuple(_finite(v) for v in values))


def load_policy(policy: FinRLPolicy | Callable[[list[float]], int] | Path | str | None = None) -> FinRLPolicy:
    """Resolve a policy: explicit object/callable > path (or FINRL_POLICY_PATH) > stub."""

    if policy is None:
        env_path = os.environ.get(POLICY_PATH_ENV)
        policy = Path(env_path) if env_path else None
    if policy is None:
        return StubPolicy()
    if isinstance(policy, str | Path):
        path = Path(policy)
        if not path.exists():
            return StubPolicy(reason=f"policy file not found: {path}")
        if path.suffix.lower() == ".json":
            return TablePolicy.from_json(path)
        return StubPolicy(reason=f"unsupported policy artifact {path.suffix!r} (expected .json table export)")
    if hasattr(policy, "act"):
        return cast(FinRLPolicy, policy)
    return CallablePolicy(fn=cast(Callable[[list[float]], int], policy))


def train_policy(bars: pd.DataFrame, *, out_path: Path, **_: Any) -> Path:
    """Training hook (not implemented this cycle).

    Recipe for the next cycle, kept here so the seam is unambiguous:
    1. ``uv add finrl stable-baselines3 gymnasium`` (heavy; keep optional).
    2. Build a ``gymnasium.Env`` whose observation is :func:`build_observation` over the
       training window and whose discrete action space is ``{HOLD, BUY, SELL}``; reward =
       bar-over-bar equity change under the engine's fill model.
    3. Train (e.g. PPO) on ``bars`` and export either a ``TablePolicy`` JSON or wrap
       ``model.predict`` in :class:`CallablePolicy`.
    4. Evaluate through ``sentinel eval run -p finrl`` with ``FINRL_POLICY_PATH`` set — the
       harness supplies the same symbols/window/metrics as every other pipeline.
    """

    _ = (bars, out_path)
    msg = "FinRL training is not wired this cycle; see sentinel/eval/finrl_adapter.py::train_policy"
    raise NotImplementedError(msg)


@dataclass
class FinRLStrategy:
    """Drive a :class:`FinRLPolicy` through the engine's rule-strategy seam (one decision per bar)."""

    policy: FinRLPolicy
    size: float = 1.0
    warmup_bars: int = 50
    actions: list[int] = field(default_factory=list)

    def on_bar(self, context: BarContext) -> Signal:
        if len(context.history) < self.warmup_bars:
            return Signal(action="HOLD", size=None)
        action = int(self.policy.act(build_observation(context)))
        self.actions.append(action)
        if action == ACTION_BUY and context.position_qty <= 0:
            return Signal(action="BUY", size=self.size)
        if action == ACTION_SELL and context.position_qty > 0:
            return Signal(action="SELL", size=1.0)
        return Signal(action="HOLD", size=None)

    @property
    def is_stub(self) -> bool:
        return isinstance(self.policy, StubPolicy)

    def notes(self) -> list[str]:
        if isinstance(self.policy, StubPolicy):
            return [f"STUB: {self.policy.reason}"]
        return [f"policy={self.policy.name}"]


def _pct_change(close: pd.Series, lag: int) -> float:
    if len(close) <= lag:
        return 0.0
    prev = float(close.iloc[-1 - lag])
    return (float(close.iloc[-1]) / prev - 1.0) if prev else 0.0


def _sma_ratio(close: pd.Series, window: int) -> float:
    if len(close) < window:
        return 0.0
    sma = float(close.iloc[-window:].mean())
    return (float(close.iloc[-1]) / sma - 1.0) if sma else 0.0


def _rolling_vol(close: pd.Series, window: int) -> float:
    if len(close) <= window:
        return 0.0
    returns = close.pct_change().iloc[-window:]
    return float(returns.std(ddof=1))


def _finite(value: float) -> float:
    return value if math.isfinite(value) else 0.0

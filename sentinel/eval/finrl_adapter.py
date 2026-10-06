"""FinRL-style reinforcement-learning baseline behind the evaluation harness.

A trained policy is driven through the *same* backtest engine, bars, fill model and
metrics as every other pipeline. Pieces:

- :class:`Observation` / :func:`build_observation` — the feature vector the policy sees on
  each bar. Training uses the vectorised twin in :mod:`sentinel.eval.rl_env`, and a test
  asserts the two agree row for row, so train and evaluation inputs cannot drift.
- :class:`FinRLPolicy` — ``act(observation) -> HOLD/BUY/SELL``. Implementations: a trained
  Stable-Baselines3 model (:class:`SB3Policy`, from ``sentinel eval train-rl``), a JSON
  threshold table, any callable, or :class:`StubPolicy` (always HOLD, flagged in results).
- :func:`train_policy` — trains the PPO baseline via :mod:`sentinel.eval.rl_train`
  (optional ``rl`` extra: ``uv sync --extra rl``).
- :class:`FinRLStrategy` — long-only, all-in strategy; ``prehistory`` supplies bars from
  before the evaluation window so rolling features are defined from day one.
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


def build_observation(context: BarContext, prehistory: pd.DataFrame | None = None) -> Observation:
    """Compute the shared feature vector from the bar history available at ``context``.

    ``prehistory`` holds bars from *before* the evaluation window so features such as the
    50-day SMA ratio are defined from the first evaluation bar, as they were in training.
    """

    close = cast(pd.Series, pd.to_numeric(_full_history(context, prehistory)["close"], errors="coerce")).astype(float)
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


@dataclass
class SB3Policy:
    """A trained Stable-Baselines3 model (``model.zip`` from ``sentinel eval train-rl``)."""

    path: Path
    name: str = "ppo"
    _model: Any = field(default=None, repr=False)

    def act(self, observation: Observation) -> int:
        import numpy as np

        if self._model is None:
            from stable_baselines3 import PPO

            self._model = PPO.load(str(self.path), device="cpu")
        obs = np.clip(np.asarray(observation.values, dtype=np.float32), -10.0, 10.0)
        action, _ = self._model.predict(obs, deterministic=True)
        return int(action)

    @classmethod
    def from_path(cls, path: Path) -> SB3Policy:
        meta_path = path.with_name("metadata.json")
        name = path.stem
        if meta_path.exists():
            name = str(json.loads(meta_path.read_text(encoding="utf-8")).get("name", name))
        return cls(path=path, name=name)


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
        if path.is_dir() and (path / "model.zip").exists():
            path = path / "model.zip"
        if path.suffix.lower() == ".json":
            return TablePolicy.from_json(path)
        if path.suffix.lower() == ".zip":
            try:
                import stable_baselines3  # noqa: F401
            except ImportError:
                return StubPolicy(reason="stable-baselines3 not installed (uv sync --extra rl)")
            return SB3Policy.from_path(path)
        return StubPolicy(reason=f"unsupported policy artifact {path.suffix!r} (expected .zip or .json)")
    if hasattr(policy, "act"):
        return cast(FinRLPolicy, policy)
    return CallablePolicy(fn=cast(Callable[[list[float]], int], policy))


def train_policy(bars: pd.DataFrame, *, out_path: Path, **kwargs: Any) -> Path:
    """Train the FinRL-style PPO baseline; returns the saved ``model.zip`` path.

    Thin wrapper over :func:`sentinel.eval.rl_train.train_ppo` (needs ``uv sync --extra rl``).
    ``kwargs`` are :class:`~sentinel.eval.rl_train.RLTrainConfig` fields.
    """

    from sentinel.eval.rl_train import RLTrainConfig, train_ppo

    return train_ppo(bars, RLTrainConfig(**kwargs), out_path)


@dataclass
class FinRLStrategy:
    """Drive a :class:`FinRLPolicy` through the engine's rule-strategy seam (one decision per bar)."""

    policy: FinRLPolicy
    size: float = 1.0
    warmup_bars: int = 50
    prehistory: pd.DataFrame | None = None
    actions: list[int] = field(default_factory=list)

    def on_bar(self, context: BarContext) -> Signal:
        if len(_full_history(context, self.prehistory)) < self.warmup_bars:
            return Signal(action="HOLD", size=None)
        action = int(self.policy.act(build_observation(context, self.prehistory)))
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


def _full_history(context: BarContext, prehistory: pd.DataFrame | None) -> pd.DataFrame:
    history = context.history
    if prehistory is None or prehistory.empty or history.empty:
        return history
    before = cast(pd.DataFrame, prehistory.loc[prehistory.index < history.index[0], ["close"]])
    return pd.concat([before, cast(pd.DataFrame, history[["close"]])])


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

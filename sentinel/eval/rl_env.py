"""FinRL-style single-asset trading environment for training the RL baseline.

The environment is deliberately built from the *same pieces* the evaluation harness uses,
so a policy trained here is evaluated under identical rules:

- observations are :data:`sentinel.eval.finrl_adapter.OBSERVATION_FEATURES`, computed by
  :func:`feature_frame` (vectorised, and tested to match ``build_observation`` row for row);
- actions are HOLD / BUY / SELL, long-only and all-in like ``FinRLStrategy`` (size 1.0);
- an action chosen on bar *t* fills at the **open of bar t+1** with the configured slippage,
  exactly as ``sentinel.backtest.engine`` fills a signal;
- equity is marked at the close; the reward is the log change in equity
  (``reward="absolute"``) or that change minus the asset's own log return
  (``reward="excess"``), so simply holding the asset earns zero;
- ``bars`` may be one frame or several (one per symbol); each episode samples a symbol,
  so the agent sees falling and sideways markets, not only one trending stock.

Requires the optional ``rl`` extra (``uv sync --extra rl``) for gymnasium.
"""

from __future__ import annotations

import math
from typing import Any, cast

import numpy as np
import pandas as pd

from sentinel.eval.finrl_adapter import ACTION_BUY, ACTION_SELL, OBSERVATION_FEATURES

try:  # gymnasium is optional; the feature code above is usable without it.
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:  # pragma: no cover - exercised only without the rl extra
    gym = None  # type: ignore[assignment]
    spaces = None  # type: ignore[assignment]

PRICE_FEATURES = OBSERVATION_FEATURES[:-1]  # everything except position_frac


def feature_frame(close: pd.Series) -> pd.DataFrame:
    """Vectorised version of ``build_observation``'s price features, one row per bar.

    Row *t* only uses closes up to and including bar *t* (no lookahead). Values that
    ``build_observation`` would report as 0.0 for lack of history are 0.0 here too.
    """

    c = cast(pd.Series, pd.to_numeric(close, errors="coerce")).astype(float)
    frame = pd.DataFrame(index=c.index)
    frame["ret_1"] = c / c.shift(1) - 1.0
    frame["ret_5"] = c / c.shift(5) - 1.0
    frame["ret_20"] = c / c.shift(20) - 1.0
    frame["sma_ratio_20"] = c / c.rolling(20).mean() - 1.0
    frame["sma_ratio_50"] = c / c.rolling(50).mean() - 1.0
    frame["vol_20"] = c.pct_change().rolling(20).std(ddof=1)
    cleaned = frame.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return cast(pd.DataFrame, cleaned[list(PRICE_FEATURES)])


def _require_gym() -> Any:
    if gym is None:
        msg = "gymnasium is not installed; run `uv sync --extra rl` to train the RL baseline"
        raise ImportError(msg)
    return gym


class TradingEnv(gym.Env if gym is not None else object):  # type: ignore[misc]
    """Long-only, all-in single-asset environment with next-open fills.

    Each episode is a random window of ``episode_length`` decision bars drawn from the
    training data (after ``warmup`` bars so every feature has full history).
    """

    metadata: dict[str, Any] = {"render_modes": []}  # noqa: RUF012 - gymnasium convention

    def __init__(
        self,
        bars: pd.DataFrame | list[pd.DataFrame],
        *,
        episode_length: int = 63,
        warmup: int = 50,
        slippage_bps: float = 5.0,
        commission_usd: float = 0.0,
        starting_cash: float = 10_000.0,
        reward: str = "absolute",
        seed: int | None = None,
    ) -> None:
        _require_gym()
        super().__init__()
        if reward not in {"absolute", "excess"}:
            msg = f"reward must be 'absolute' or 'excess', got {reward!r}"
            raise ValueError(msg)
        frames = bars if isinstance(bars, list) else [bars]
        self._series: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        for raw in frames:
            frame = raw.copy()
            frame.columns = [str(col).lower() for col in frame.columns]
            if len(frame) < warmup + episode_length + 2:
                continue
            self._series.append(
                (
                    frame["open"].to_numpy(float),
                    frame["close"].to_numpy(float),
                    feature_frame(cast(pd.Series, frame["close"])).to_numpy(np.float32),
                )
            )
        if not self._series:
            msg = f"need at least one series with {warmup + episode_length + 2} bars"
            raise ValueError(msg)
        self.open, self.close, self.features = self._series[0]
        self.reward_mode = reward
        self.episode_length = episode_length
        self.warmup = warmup
        self.slip = slippage_bps / 10_000.0
        self.commission = commission_usd
        self.starting_cash = starting_cash
        n_obs = len(OBSERVATION_FEATURES)
        assert spaces is not None  # guaranteed by _require_gym()
        self.observation_space = spaces.Box(low=-10.0, high=10.0, shape=(n_obs,), dtype=np.float32)
        self.action_space = spaces.Discrete(3)
        self._rng = np.random.default_rng(seed)
        self._t = warmup
        self._end = warmup + episode_length
        self.cash = starting_cash
        self.shares = 0.0
        self.trades = 0

    # -- gymnasium API -------------------------------------------------------------------
    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        which = 0 if options and options.get("from_start") else int(self._rng.integers(len(self._series)))
        self.open, self.close, self.features = self._series[which]
        last_start = len(self.close) - self.episode_length - 1
        start = self.warmup if options and options.get("from_start") else int(
            self._rng.integers(self.warmup, last_start + 1)
        )
        self._t = start
        self._end = start + self.episode_length
        self.cash = self.starting_cash
        self.shares = 0.0
        self.trades = 0
        return self._observation(), {}

    def step(self, action: int):
        t = self._t
        equity_before = self._equity(self.close[t])
        fill_price = self.open[t + 1]
        if action == ACTION_BUY and self.shares <= 0 and self.cash > self.commission:
            price = fill_price * (1.0 + self.slip)
            self.shares = (self.cash - self.commission) / price
            self.cash = 0.0
            self.trades += 1
        elif action == ACTION_SELL and self.shares > 0:
            price = fill_price * (1.0 - self.slip)
            self.cash += self.shares * price - self.commission
            self.shares = 0.0
            self.trades += 1
        self._t = t + 1
        equity_after = self._equity(self.close[self._t])
        reward = math.log(max(equity_after, 1e-9) / max(equity_before, 1e-9))
        if self.reward_mode == "excess":
            reward -= math.log(self.close[self._t] / self.close[t])
        terminated = self._t >= self._end
        info = {"equity": equity_after, "trades": self.trades}
        return self._observation(), float(reward), terminated, False, info

    # -- helpers ----------------------------------------------------------------------------
    def _equity(self, mark: float) -> float:
        return self.cash + self.shares * mark

    def _observation(self) -> np.ndarray:
        equity = self._equity(self.close[self._t])
        position_frac = (self.shares * self.close[self._t] / equity) if equity > 0 else 0.0
        obs = np.append(self.features[self._t], np.float32(position_frac)).astype(np.float32)
        return cast(np.ndarray, np.clip(obs, -10.0, 10.0))

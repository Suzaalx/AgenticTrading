from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sentinel.backtest.strategies.base import BarContext
from sentinel.eval.finrl_adapter import (
    ACTION_BUY,
    ACTION_HOLD,
    ACTION_SELL,
    FinRLStrategy,
    StubPolicy,
    build_observation,
    load_policy,
)
from sentinel.eval.rl_env import PRICE_FEATURES, feature_frame
from tests.eval.test_experiment import synthetic_bars


def _ctx(bars: pd.DataFrame, upto: int) -> BarContext:
    history = bars.iloc[:upto]
    return BarContext(
        symbol="NVDA", timestamp=history.index[-1], bar=history.iloc[-1], history=history,
        cash=10_000.0, position_qty=0.0, position_value=0.0, equity=10_000.0,
    )


def test_vectorised_features_match_build_observation_row_for_row() -> None:
    """Training (feature_frame) and evaluation (build_observation) must see identical inputs."""

    bars = synthetic_bars(160)
    frame = feature_frame(bars["close"])
    for t in (0, 1, 5, 19, 20, 21, 49, 50, 51, 100, 159):
        obs = build_observation(_ctx(bars, t + 1))
        expected = dict(zip(obs.names, obs.values, strict=True))
        for name in PRICE_FEATURES:
            assert frame.iloc[t][name] == pytest.approx(expected[name], abs=1e-9), (t, name)


def test_prehistory_makes_features_defined_from_the_first_window_bar() -> None:
    bars = synthetic_bars(160)
    pre, window = bars.iloc[:80], bars.iloc[80:]
    without = build_observation(_ctx(window, 1))
    with_pre = build_observation(_ctx(window, 1), prehistory=pre)
    full = build_observation(_ctx(bars, 81))

    assert dict(zip(without.names, without.values, strict=True))["sma_ratio_50"] == 0.0
    assert with_pre.values == pytest.approx(full.values)


def test_strategy_with_prehistory_acts_on_day_one() -> None:
    bars = synthetic_bars(160)
    pre, window = bars.iloc[:80], bars.iloc[80:]

    class AlwaysBuy:
        name = "buy"

        def act(self, observation):
            return ACTION_BUY

    assert FinRLStrategy(policy=AlwaysBuy(), prehistory=pre).on_bar(_ctx(window, 1)).action == "BUY"
    assert FinRLStrategy(policy=AlwaysBuy()).on_bar(_ctx(window, 1)).action == "HOLD"  # still warming up


def test_load_policy_handles_zip_and_directories(tmp_path: Path) -> None:
    missing = load_policy(tmp_path / "nope.zip")
    assert isinstance(missing, StubPolicy)
    (tmp_path / "model.zip").write_bytes(b"not really a model")
    resolved = load_policy(tmp_path)  # a training output directory
    pytest.importorskip("stable_baselines3")
    assert type(resolved).__name__ == "SB3Policy"


gym = pytest.importorskip("gymnasium", reason="RL extra not installed")


def test_env_fills_at_next_open_with_slippage_and_rewards_log_equity() -> None:
    from sentinel.eval.rl_env import TradingEnv

    bars = synthetic_bars(160)
    env = TradingEnv(bars, episode_length=10, warmup=50, slippage_bps=5.0)
    obs, _ = env.reset(options={"from_start": True})
    assert obs.shape == (7,) and obs[-1] == 0.0  # flat

    t = env._t
    _, reward, terminated, _, info = env.step(ACTION_BUY)
    fill = bars["open"].iloc[t + 1] * 1.0005
    expected_equity = 10_000.0 / fill * bars["close"].iloc[t + 1]
    assert info["equity"] == pytest.approx(expected_equity)
    assert reward == pytest.approx(np.log(expected_equity / 10_000.0))
    assert not terminated

    obs, _, _, _, _ = env.step(ACTION_HOLD)
    assert obs[-1] == pytest.approx(1.0)  # fully invested
    _, _, _, _, info = env.step(ACTION_SELL)
    assert info["trades"] == 2 and env.shares == 0.0


def test_env_episodes_terminate_and_never_read_past_the_data() -> None:
    from sentinel.eval.rl_env import TradingEnv

    bars = synthetic_bars(120)
    env = TradingEnv(bars, episode_length=63, warmup=50, seed=3)
    for _ in range(20):
        env.reset()
        done, steps = False, 0
        while not done:
            _, _, done, _, _ = env.step(env.action_space.sample())
            steps += 1
        assert steps == 63 and env._t < len(bars)


def test_train_ppo_end_to_end_tiny_run(tmp_path: Path) -> None:
    pytest.importorskip("stable_baselines3")
    from sentinel.eval.rl_train import RLTrainConfig, train_ppo, training_curve

    bars = synthetic_bars(200)
    cfg = RLTrainConfig(
        symbol="NVDA", train_start=bars.index[0].date(), train_end=bars.index[-1].date(),
        timesteps=256, seed=1, episode_length=40, ppo_kwargs={"n_steps": 64, "batch_size": 32},
    )
    model_path = train_ppo(bars, cfg, tmp_path / "ppo")

    meta = json.loads((tmp_path / "ppo" / "metadata.json").read_text())
    assert model_path.exists() and meta["timesteps"] == 256 and meta["seed"] == 1
    assert len(training_curve(tmp_path / "ppo")) >= 1
    policy = load_policy(tmp_path / "ppo")
    assert policy.name == "ppo_nvda_absolute_s1"
    assert policy.act(build_observation(_ctx(bars, 120))) in {ACTION_HOLD, ACTION_BUY, ACTION_SELL}


def test_train_ppo_refuses_overlapping_windows(tmp_path: Path) -> None:
    pytest.importorskip("stable_baselines3")
    from sentinel.eval.rl_train import RLTrainConfig, train_ppo

    cfg = RLTrainConfig(symbol="NVDA", train_start=date(2021, 1, 1), train_end=date(2025, 3, 31), eval_start=date(2025, 3, 3))
    with pytest.raises(ValueError, match="overlaps"):
        train_ppo(synthetic_bars(200), cfg, tmp_path)


def test_excess_reward_makes_buy_and_hold_worth_zero() -> None:
    from sentinel.eval.rl_env import TradingEnv

    bars = synthetic_bars(160)
    env = TradingEnv(bars, episode_length=30, warmup=50, slippage_bps=0.0, reward="excess")
    env.reset(options={"from_start": True})
    env.step(ACTION_BUY)  # enters at the next open: first step still carries the open->close gap
    rewards = [env.step(ACTION_HOLD)[1] for _ in range(20)]
    assert all(abs(r) < 1e-9 for r in rewards)  # fully invested == the asset itself -> zero excess


def test_env_samples_across_several_symbols() -> None:
    from sentinel.eval.rl_env import TradingEnv

    a, b = synthetic_bars(150, seed=1), synthetic_bars(150, seed=2) * 3
    env = TradingEnv([a, b], episode_length=30, warmup=50, seed=0)
    seen = set()
    for _ in range(20):
        env.reset()
        seen.add(round(float(env.close[0]), 4))
    assert len(seen) == 2
    with pytest.raises(ValueError, match="reward must be"):
        TradingEnv(a, reward="sharpe")

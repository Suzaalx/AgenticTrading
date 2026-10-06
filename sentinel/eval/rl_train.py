"""Train the FinRL-style PPO baseline (Stable-Baselines3) on a fixed training window.

Outputs, all in ``out_dir``:

- ``model.zip``       the trained PPO policy (load with ``load_policy(path)``)
- ``metadata.json``   symbol, windows, seed, timesteps, features, fill settings
- ``monitor.csv``     one row per training episode (episode return, length) — the
                      training curve used as evidence in the report

The training window must end before the evaluation window starts; ``train_ppo`` refuses
overlapping windows so the reported result is out-of-sample.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from sentinel.eval.finrl_adapter import OBSERVATION_FEATURES
from sentinel.eval.rl_env import TradingEnv


@dataclass(frozen=True)
class RLTrainConfig:
    symbol: str
    train_start: date
    train_end: date
    timesteps: int = 100_000
    seed: int = 0
    episode_length: int = 63
    slippage_bps: float = 5.0
    commission_usd: float = 0.0
    eval_start: date | None = None  # if given, must be after train_end
    reward: str = "absolute"  # "absolute" or "excess" (vs holding the asset)
    train_symbols: tuple[str, ...] = ()  # extra symbols mixed into training episodes
    ppo_kwargs: dict[str, object] = field(default_factory=dict)


def train_ppo(bars: pd.DataFrame | list[pd.DataFrame], cfg: RLTrainConfig, out_dir: Path) -> Path:
    """Train PPO on ``bars`` (one frame, or one per symbol) and save artifacts."""

    from stable_baselines3 import PPO
    from stable_baselines3.common.monitor import Monitor

    if cfg.eval_start is not None and cfg.eval_start <= cfg.train_end:
        msg = f"training window ends {cfg.train_end}, which overlaps evaluation start {cfg.eval_start}"
        raise ValueError(msg)
    frames = bars if isinstance(bars, list) else [bars]
    lo, hi = pd.Timestamp(cfg.train_start), pd.Timestamp(cfg.train_end)
    windows = [frame.loc[(frame.index >= lo) & (frame.index <= hi)] for frame in frames]
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Monitor(
        TradingEnv(
            windows,
            episode_length=cfg.episode_length,
            slippage_bps=cfg.slippage_bps,
            commission_usd=cfg.commission_usd,
            reward=cfg.reward,
            seed=cfg.seed,
        ),
        filename=str(out_dir),  # SB3 writes <dir>/monitor.csv
    )
    model = PPO("MlpPolicy", env, seed=cfg.seed, verbose=0, **cfg.ppo_kwargs)  # type: ignore[arg-type]
    model.learn(total_timesteps=cfg.timesteps)
    model_path = out_dir / "model.zip"
    model.save(str(model_path))
    meta = {
        **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(cfg).items()},
        "name": f"ppo_{cfg.symbol.lower()}_{cfg.reward}{'_multi' if cfg.train_symbols else ''}_s{cfg.seed}",
        "algorithm": "PPO (stable-baselines3, MlpPolicy)",
        "features": list(OBSERVATION_FEATURES),
        "training_bars": int(sum(len(w) for w in windows)),
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return model_path


def training_curve(out_dir: Path) -> pd.DataFrame:
    """Read the Monitor log as a DataFrame with ``episode``, ``reward`` and ``length`` columns."""

    frame = pd.read_csv(Path(out_dir) / "monitor.csv", skiprows=1)
    frame = frame.rename(columns={"r": "reward", "l": "length", "t": "seconds"})
    frame.insert(0, "episode", range(1, len(frame) + 1))
    return frame

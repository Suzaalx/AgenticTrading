"""Typed config and mandate loading."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, cast

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentinel.core.models import Mandate


class LLMSettings(BaseSettings):
    provider: str = "anthropic"
    deep_model: str = "claude-sonnet-5"
    quick_model: str = "claude-haiku-4-5-20251001"
    temperature: float = 0.0
    max_retries: int = 3
    monthly_budget_usd: float = 25.0


class PipelineSettings(BaseSettings):
    max_debate_rounds: int = 2
    max_risk_discuss_rounds: int = 1
    always_run_risk_debate: bool = False
    depth_presets: dict[str, int] = Field(
        default_factory=lambda: {"fast": 1, "standard": 2, "deep": 3}
    )


class DataSettings(BaseSettings):
    alpha_vantage_key_env: str = "ALPHA_VANTAGE_KEY"
    finnhub_key_env: str = "FINNHUB_KEY"
    news_limit: int = 20
    news_lookback_days: int = 7


class ExecutionSettings(BaseSettings):
    slippage_bps: int = 5
    commission_usd: float = 0.0
    starting_cash_usd: float = 10000.0


class ScheduleSettings(BaseSettings):
    enabled: bool = False
    decision_cron: str = "30 9 * * 1-5"
    watchlist: list[str] = Field(default_factory=lambda: ["NVDA", "AAPL", "SPY"])


class MonitorSettings(BaseSettings):
    equity_interval_min: int = 5
    crypto_interval_min: int = 15


class Settings(BaseSettings):
    """Complete Sentinel config from defaults, config.toml, .env, and env vars."""

    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)


def _toml_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return cast(dict[str, Any], tomllib.load(f))


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _coerce(value: str, default: Any) -> Any:
    if isinstance(default, bool):
        return value.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def _overlay_env(data: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    merged = {section: dict(values) for section, values in data.items() if isinstance(values, dict)}
    defaults = Settings.model_validate(data).model_dump()
    for section, values in defaults.items():
        section_map = merged.setdefault(section, {})
        if not isinstance(values, dict):
            continue
        for field_name, default in values.items():
            candidates = (
                f"SENTINEL_{section}__{field_name}".upper(),
                f"SENTINEL_{section}_{field_name}".upper(),
                f"{section}_{field_name}".upper(),
            )
            for key in candidates:
                if key in env:
                    section_map[field_name] = _coerce(env[key], default)
                    break
    return merged


def load_settings(root: Path | None = None) -> Settings:
    """Load Settings with precedence env > config.toml > defaults."""

    base = root or Path.cwd()
    data = _toml_data(base / "config.toml")
    env = {**_parse_env_file(base / ".env"), **os.environ}
    return Settings.model_validate(_overlay_env(data, env))


def load_mandate(root: Path | None = None) -> Mandate:
    """Load and validate mandate.toml from a project root."""

    base = root or Path.cwd()
    data = _toml_data(base / "mandate.toml")
    mandate_data = data.get("mandate", data)
    if not isinstance(mandate_data, dict):
        msg = "mandate.toml must contain a [mandate] table"
        raise ValueError(msg)
    return Mandate.model_validate(mandate_data)

"""Typed config and mandate loading."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, cast

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentinel.core.models import Mandate


class LLMProviderSettings(BaseSettings):
    """Optional per-provider overrides for OpenAI-compatible open-weight backends."""

    base_url: str | None = None
    api_key_env: str | None = None


class LLMSettings(BaseSettings):
    provider: str = "groq"
    deep_model: str = "openai/gpt-oss-120b"
    quick_model: str = "openai/gpt-oss-20b"
    role_models: dict[str, str] = Field(default_factory=dict)
    temperature: float = 0.0
    max_tokens: int = 4096
    max_retries: int = 3
    monthly_budget_usd: float = 25.0
    providers: dict[str, LLMProviderSettings] = Field(default_factory=dict)


class PipelineSettings(BaseSettings):
    # Ablation switch: false skips the bull/bear debate + research manager and routes analyst
    # outputs straight to the trader via a deterministic roll-up (no extra LLM call).
    debate_enabled: bool = True
    # Bars of OHLCV + indicators rendered into the market-analyst prompt (SPEC default 90).
    # Free hosted tiers meter prompt + reserved max_tokens against a per-minute budget, so
    # the shipped config.toml lowers this; paid providers can restore 90.
    analyst_history_bars: int = 90
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


class OptionsSettings(BaseSettings):
    enabled: bool = False
    commission_per_contract_usd: float = 0.65
    slippage_half_spread_frac: float = 0.25
    force_close_dte: int = 1
    default_risk_free_rate: float = 0.04


class BacktestOptionsSettings(BaseSettings):
    iv_premium_factor: float = 1.1
    synthetic_half_spread_pct: float = 2.0


class BacktestSettings(BaseSettings):
    options: BacktestOptionsSettings = Field(default_factory=BacktestOptionsSettings)


class LiveSettings(BaseSettings):
    live_order_ttl_minutes: int = 30
    marketable_limit_buffer_bps: int = 10
    reconcile_interval_min: int = 5


class RobinhoodSettings(BaseSettings):
    enabled: bool = False
    crypto_only: bool = True
    api_key_env: str = "ROBINHOOD_API_KEY"
    private_key_env: str = "ROBINHOOD_PRIVATE_KEY"
    base_url: str = "https://trading.robinhood.com"
    allowed_symbols: set[str] = Field(default_factory=lambda: {"BTC-USD", "ETH-USD"})
    request_timeout_seconds: float = 10.0


class RobinhoodAgenticSettings(BaseSettings):
    enabled: bool = False
    mcp_endpoint_env: str = "RH_AGENTIC_MCP_URL"
    mcp_token_env: str = "RH_AGENTIC_MCP_TOKEN"
    agentic_account_number: str = ""
    use_claude_bridge: bool = True


class ExecutionSettings(BaseSettings):
    slippage_bps: int = 5
    commission_usd: float = 0.0
    starting_cash_usd: float = 10000.0
    robinhood: RobinhoodSettings = Field(default_factory=RobinhoodSettings)
    robinhood_agentic: RobinhoodAgenticSettings = Field(
        default_factory=RobinhoodAgenticSettings
    )


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
    options: OptionsSettings = Field(default_factory=OptionsSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    live: LiveSettings = Field(default_factory=LiveSettings)
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
        value = value.strip()
        if not (value.startswith('"') or value.startswith("'")) and " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value.strip('"').strip("'")
    return values


def export_env_file(root: Path | None = None) -> dict[str, str]:
    """Load ``.env`` into ``os.environ`` (without overriding real env vars) and return it.

    Provider adapters read API keys from ``os.environ``; this keeps ".env only" workable
    without asking users to export keys in their shell.
    """

    base = root or Path.cwd()
    values = _parse_env_file(base / ".env")
    for key, value in values.items():
        if value:
            os.environ.setdefault(key, value)
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
    env = {**export_env_file(base), **os.environ}
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

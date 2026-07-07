from __future__ import annotations

import pytest

from sentinel.config.settings import load_mandate, load_settings
from sentinel.core.models import Mandate

BASE_MANDATE = """
[mandate]
symbol_universe = ["AAPL"]
max_position_pct_equity = 10.0
max_order_notional_usd = 2000.0
max_gross_exposure_pct = 80.0
max_daily_loss_pct = 3.0
max_orders_per_day = 10
allow_short = false
cooldown_minutes_per_symbol = 60
"""


def test_defined_risk_only_false_is_rejected() -> None:
    with pytest.raises(ValueError, match="defined_risk_only"):
        Mandate.model_validate(
            {
                "symbol_universe": ["AAPL"],
                "max_position_pct_equity": 10.0,
                "max_order_notional_usd": 2000.0,
                "max_gross_exposure_pct": 80.0,
                "max_daily_loss_pct": 3.0,
                "max_orders_per_day": 10,
                "allow_short": False,
                "cooldown_minutes_per_symbol": 60,
                "options": {"defined_risk_only": False},
            }
        )


def test_load_mandate_with_and_without_new_sections(tmp_path) -> None:
    (tmp_path / "mandate.toml").write_text(BASE_MANDATE, encoding="utf-8")
    defaulted = load_mandate(tmp_path)
    assert defaulted.options.enabled is False
    assert defaulted.live.require_limit_orders is True

    (tmp_path / "mandate.toml").write_text(
        BASE_MANDATE
        + """
[mandate.options]
enabled = true
underlying_universe = ["SPY"]
max_loss_per_position_usd = 250.0

[mandate.live]
crypto_stage_enabled = true
max_live_order_notional_usd = 50.0
""",
        encoding="utf-8",
    )
    loaded = load_mandate(tmp_path)
    assert loaded.options.enabled is True
    assert loaded.options.underlying_universe == ["SPY"]
    assert loaded.options.max_loss_per_position_usd == 250.0
    assert loaded.live.crypto_stage_enabled is True
    assert loaded.live.max_live_order_notional_usd == 50.0


def test_load_settings_with_and_without_option_sections(tmp_path) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    defaulted = load_settings(tmp_path)
    assert defaulted.options.enabled is False
    assert defaulted.backtest.options.iv_premium_factor == 1.1
    assert defaulted.live.live_order_ttl_minutes == 30
    assert defaulted.execution.robinhood_agentic.enabled is False

    (tmp_path / "config.toml").write_text(
        """
[options]
enabled = true
default_risk_free_rate = 0.05

[backtest.options]
synthetic_half_spread_pct = 3.0

[live]
reconcile_interval_min = 2

[execution.robinhood_agentic]
enabled = true
mcp_endpoint_env = "CUSTOM_RH_MCP"
""".strip(),
        encoding="utf-8",
    )
    loaded = load_settings(tmp_path)
    assert loaded.options.enabled is True
    assert loaded.options.default_risk_free_rate == 0.05
    assert loaded.backtest.options.synthetic_half_spread_pct == 3.0
    assert loaded.live.reconcile_interval_min == 2
    assert loaded.execution.robinhood_agentic.mcp_endpoint_env == "CUSTOM_RH_MCP"

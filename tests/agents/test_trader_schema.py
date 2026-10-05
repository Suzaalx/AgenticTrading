from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.agents.trader import Trader
from sentinel.orchestrator.state import initial_state


def test_equity_runs_send_a_schema_that_requires_quantity_pct() -> None:
    state = initial_state("NVDA")  # no option candidates
    schema = Trader.schema_for(Trader.__new__(Trader), state)

    assert schema is Trader.EquityPayload
    assert "quantity_pct" in schema.model_json_schema()["required"]


def test_equity_payload_rejects_missing_quantity_and_defaults_order_type() -> None:
    base = {"content": "Buy", "action": "BUY", "time_horizon_days": 5, "entry_rationale": "r", "exit_plan": "e"}

    with pytest.raises(ValidationError):
        Trader.EquityPayload.model_validate(base)  # the failure mode seen in the pilot
    ok = Trader.EquityPayload.model_validate({**base, "quantity_pct": 40})
    assert ok.order_type == "market"


def test_option_runs_keep_the_union_payload() -> None:
    # Any non-empty candidate list switches the trader to the union payload.
    state = initial_state("NVDA").model_copy(update={"option_candidates": [object()]})

    assert Trader.schema_for(Trader.__new__(Trader), state) is Trader.Payload

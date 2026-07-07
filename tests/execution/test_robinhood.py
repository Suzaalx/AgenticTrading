from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.config.settings import RobinhoodSettings, load_settings
from sentinel.core.models import Order
from sentinel.execution.broker import OrderRejected
from sentinel.execution.robinhood import RobinhoodCryptoBroker


class StubClient:
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, *args: object, **kwargs: object) -> None:
        self.calls += 1


def _order(symbol: str = "BTC-USD") -> Order:
    return Order(
        order_id=f"order-{symbol}",
        run_id="run-1",
        symbol=symbol,
        side="buy",
        qty=Decimal("0.01"),
        type="market",
        reason="agent_decision",
        created_at=datetime(2026, 7, 7, 8, 0, tzinfo=UTC),
    )


def test_constructor_disabled_raises() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        RobinhoodCryptoBroker(RobinhoodSettings(enabled=False))


@pytest.mark.asyncio
async def test_submit_rejects_non_btc_eth_symbol_before_network() -> None:
    client = StubClient()
    broker = RobinhoodCryptoBroker(RobinhoodSettings(enabled=True), client=client)

    result = await broker.submit(_order("DOGE-USD"))

    assert isinstance(result, OrderRejected)
    assert result.reason == "unsupported robinhood crypto symbol: DOGE-USD"
    assert client.calls == 0


@pytest.mark.asyncio
async def test_allowed_submit_and_get_quote_are_inert_stubs() -> None:
    client = StubClient()
    broker = RobinhoodCryptoBroker(RobinhoodSettings(enabled=True), client=client)

    with pytest.raises(NotImplementedError, match="not wired"):
        await broker.submit(_order("BTC-USD"))
    with pytest.raises(NotImplementedError, match="not wired"):
        await broker.get_quote("ETH-USD")
    assert client.calls == 0


def test_robinhood_settings_default_disabled(tmp_path) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    settings = load_settings(tmp_path)

    assert settings.execution.robinhood.enabled is False

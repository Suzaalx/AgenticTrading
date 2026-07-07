from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from typing import ClassVar

import pandas as pd

from sentinel.data.loaders.yfinance_options import YFinanceOptionsLoader


def _frame(kind: str) -> pd.DataFrame:
    prefix = "C" if kind == "call" else "P"
    return pd.DataFrame(
        {
            "strike": [70.0, 100.0, 130.0],
            "lastPrice": [30.0, 2.4, 31.0],
            "bid": [29.0, 2.0, 30.0],
            "ask": [31.0, 3.0, 32.0],
            "volume": [10, 20, 30],
            "openInterest": [100, 200, 300],
            "impliedVolatility": [0.25, 0.20, 0.25],
            "contractSymbol": [f"NVDA260220{prefix}00070000", f"NVDA260220{prefix}00100000", f"NVDA260220{prefix}00130000"],
            "lastTradeDate": ["2026-01-01T15:30:00Z"] * 3,
        }
    )


def test_yfinance_options_loader_builds_quotes_greeks_and_trims(
    monkeypatch,
) -> None:
    class FakeTicker:
        options: ClassVar[list[str]] = ["2026-02-20"]

        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def option_chain(self, expiry: str) -> object:
            assert expiry == "2026-02-20"
            return types.SimpleNamespace(calls=_frame("call"), puts=_frame("put"))

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    loaded = YFinanceOptionsLoader().load_option_chain(
        "NVDA",
        datetime(2026, 1, 1, tzinfo=UTC),
        100.0,
        0.04,
        0.0,
        min_dte=21,
        max_dte=60,
    )

    assert loaded is not None
    assert len(loaded.quotes) == 2
    assert {float(quote.contract.strike) for quote in loaded.quotes} == {100.0}
    assert all(quote.delta is not None for quote in loaded.quotes)
    assert all(quote.gamma is not None for quote in loaded.quotes)
    assert all(quote.vega is not None for quote in loaded.quotes)
    assert all(quote.theta is not None for quote in loaded.quotes)


def test_yfinance_options_loader_sanity_drops_bad_rows_and_nulls_absurd_iv(
    monkeypatch,
) -> None:
    class FakeTicker:
        options: ClassVar[list[str]] = ["2026-02-20"]

        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def option_chain(self, expiry: str) -> object:
            _ = expiry
            calls = pd.DataFrame(
                {
                    "strike": [95.0, 100.0, 105.0, 110.0],
                    "lastPrice": [2.0, 2.0, 2.0, 2.0],
                    "bid": [3.0, 1.0, -1.0, 2.0],
                    "ask": [2.0, 0.0, 2.0, 3.0],
                    "volume": [1, 1, 1, 1],
                    "openInterest": [1, 1, 1, 1],
                    "impliedVolatility": [0.2, 0.2, 0.2, 9.0],
                    "contractSymbol": ["BAD1", "BAD2", "BAD3", "GOOD"],
                    "lastTradeDate": ["2026-01-01T15:30:00Z"] * 4,
                }
            )
            return types.SimpleNamespace(calls=calls, puts=pd.DataFrame())

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    loaded = YFinanceOptionsLoader().load_option_chain(
        "NVDA",
        datetime(2026, 1, 1, tzinfo=UTC),
        100.0,
        0.04,
        0.0,
        min_dte=21,
        max_dte=60,
    )

    assert loaded is not None
    assert [quote.contract.contract_symbol for quote in loaded.quotes] == ["GOOD"]
    assert loaded.quotes[0].implied_vol is None


def test_yfinance_options_loader_returns_none_on_provider_error(monkeypatch) -> None:
    class FakeTicker:
        @property
        def options(self) -> list[str]:
            raise RuntimeError("provider down")

        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    loaded = YFinanceOptionsLoader().load_option_chain(
        "NVDA",
        datetime(2026, 1, 1, tzinfo=UTC),
        100.0,
        0.04,
        0.0,
        min_dte=21,
        max_dte=60,
    )

    assert loaded is None

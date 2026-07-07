from __future__ import annotations

from datetime import date

import pandas as pd

from sentinel.data.loaders.stooq import StooqLoader


def test_stooq_loader_suffixes_us_equity_symbols(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_reader(symbol: str, provider: str, start: date, end: date) -> pd.DataFrame:
        captured["symbol"] = symbol
        captured["provider"] = provider
        _ = (start, end)
        return pd.DataFrame(
            {
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [1000],
            },
            index=pd.date_range("2025-01-02", periods=1, name="Date"),
        )

    from pandas_datareader import data as pdr

    monkeypatch.setattr(pdr, "DataReader", fake_reader)

    frame = StooqLoader().get_ohlcv("AAPL", date(2025, 1, 1), date(2025, 1, 3))

    assert captured == {"symbol": "AAPL.US", "provider": "stooq"}
    assert float(frame.iloc[0]["close"]) == 10.5


def test_stooq_loader_preserves_dotted_and_crypto_symbols(monkeypatch) -> None:
    captured: list[str] = []

    def fake_reader(symbol: str, provider: str, start: date, end: date) -> pd.DataFrame:
        captured.append(symbol)
        _ = (provider, start, end)
        return pd.DataFrame(
            {
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [1000],
            },
            index=pd.date_range("2025-01-02", periods=1, name="Date"),
        )

    from pandas_datareader import data as pdr

    monkeypatch.setattr(pdr, "DataReader", fake_reader)

    loader = StooqLoader()
    loader.get_ohlcv("BRK.B", date(2025, 1, 1), date(2025, 1, 3))
    loader.get_ohlcv("BTC-USD", date(2025, 1, 1), date(2025, 1, 3))

    assert captured == ["BRK.B", "BTC-USD"]

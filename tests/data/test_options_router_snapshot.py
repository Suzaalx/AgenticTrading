from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from sentinel.core.models import OptionContract, OptionQuote, Quote
from sentinel.data.loaders.rates import RateResult
from sentinel.data.loaders.yfinance_options import LoadedOptionChain
from sentinel.data.router import DataRouter
from sentinel.data.snapshot import compute_iv_context, compute_yang_zhang_from_ohlcv


class QuoteFake:
    name = "yfinance"

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol.upper(),
            price=Decimal("100"),
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            source=self.name,
        )

    def get_fundamentals(self, symbol: str) -> None:
        _ = symbol
        return None


class OptionFake:
    name = "yfinance_options"

    def load_option_chain(
        self,
        underlying: str,
        as_of: datetime,
        spot: float,
        risk_free_rate: float,
        dividend_yield: float,
        *,
        min_dte: int,
        max_dte: int,
    ) -> LoadedOptionChain:
        _ = (as_of, spot, risk_free_rate, dividend_yield, min_dte, max_dte)
        contract = OptionContract(
            contract_symbol="NVDA260220C00100000",
            underlying=underlying.upper(),
            kind="call",
            strike=Decimal("100"),
            expiry=date(2026, 2, 20),
        )
        quote = OptionQuote(
            contract=contract,
            bid=Decimal("2"),
            ask=Decimal("3"),
            last=Decimal("2.5"),
            volume=10,
            open_interest=100,
            implied_vol=0.2,
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            source=self.name,
            model_iv=0.21,
            delta=0.5,
            gamma=0.02,
            vega=10.0,
            theta=-5.0,
        )
        return LoadedOptionChain(expiries=[date(2026, 2, 20)], quotes=[quote])


def test_get_option_chain_returns_live_chain(monkeypatch) -> None:
    monkeypatch.setattr(
        "sentinel.data.router.risk_free_rate",
        lambda *args, **kwargs: RateResult(0.04, "fixture"),
    )
    router = DataRouter(loaders=[QuoteFake(), OptionFake()])

    chain = router.get_option_chain("NVDA", datetime(2026, 1, 1, tzinfo=UTC))

    assert chain is not None
    assert chain.pricing_source == "live_chain"
    assert chain.providers_used["option_chain"] == "yfinance_options"


def test_iv_context_rank_percentile() -> None:
    rank, percentile = compute_iv_context(0.30, [0.10, 0.20, 0.40, 0.50])

    assert rank == pytest.approx(0.5)
    assert percentile == 0.5


def test_yang_zhang_from_ohlcv_sanity() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "volume": [1000, 1000, 1000, 1000],
        },
        index=pd.date_range("2026-01-01", periods=4),
    )

    rv = compute_yang_zhang_from_ohlcv(frame)

    assert rv is not None
    assert rv > 0.0

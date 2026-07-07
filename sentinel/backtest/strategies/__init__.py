"""Deterministic rule strategies for Sentinel backtests."""

from sentinel.backtest.strategies.base import BarContext, Strategy
from sentinel.backtest.strategies.buy_hold import BuyHoldStrategy
from sentinel.backtest.strategies.rsi_meanrevert import RSIMeanRevertStrategy
from sentinel.backtest.strategies.sma_cross import SMACrossStrategy

__all__ = [
    "BarContext",
    "BuyHoldStrategy",
    "RSIMeanRevertStrategy",
    "SMACrossStrategy",
    "Strategy",
]

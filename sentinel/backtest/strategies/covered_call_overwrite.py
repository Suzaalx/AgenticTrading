"""Covered-call overwrite option rule strategy."""

from __future__ import annotations

from sentinel.backtest.strategies.base import BarContext
from sentinel.backtest.synthetic_chain import OptionRuleSignal


class CoveredCallOverwriteStrategy:
    """Maintain 100-share lots and sell an approximately 30-delta call each cycle."""

    def __init__(self, target_delta: float = 0.30, target_dte: int = 30, contracts: int = 1) -> None:
        self.target_delta = target_delta
        self.target_dte = target_dte
        self.contracts = contracts
        self._has_open_options = False

    def sync_option_state(self, has_open_options: bool, _shares: float) -> None:
        self._has_open_options = has_open_options

    def on_bar(self, context: BarContext) -> OptionRuleSignal:
        if self._has_open_options:
            return OptionRuleSignal()
        if context.cash + context.position_value <= 0:
            return OptionRuleSignal()
        return OptionRuleSignal(
            action="OPEN_COVERED_CALL",
            strategy="covered_call_overwrite",
            target_delta=self.target_delta,
            target_dte=self.target_dte,
            contracts=self.contracts,
        )

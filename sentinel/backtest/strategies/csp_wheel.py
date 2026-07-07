"""Cash-secured-put wheel option rule strategy."""

from __future__ import annotations

from sentinel.backtest.strategies.base import BarContext
from sentinel.backtest.synthetic_chain import OptionRuleSignal


class CSPWheelStrategy:
    """Sell cash-secured puts, then covered calls after assignment."""

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
        if context.position_qty >= 100 * self.contracts:
            return OptionRuleSignal(
                action="OPEN_COVERED_CALL",
                strategy="csp_wheel",
                target_delta=self.target_delta,
                target_dte=self.target_dte,
                contracts=self.contracts,
            )
        return OptionRuleSignal(
            action="OPEN_CASH_SECURED_PUT",
            strategy="csp_wheel",
            target_delta=self.target_delta,
            target_dte=self.target_dte,
            contracts=self.contracts,
        )

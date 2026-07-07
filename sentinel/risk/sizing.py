"""Deterministic position sizing utilities."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal

from sentinel.core.models import Mandate, OptionPosition, Portfolio

ONE_HUNDRED = Decimal("100")
CRYPTO_QUANTUM = Decimal("0.000001")


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _non_negative(value: Decimal) -> Decimal:
    return max(value, Decimal("0"))


def pm_scale(trader_quantity_pct: Decimal | float, approved_quantity_pct: Decimal | float) -> Decimal:
    """Return approved/trader scale clamped to [0, 1]."""

    trader = _non_negative(_decimal(trader_quantity_pct))
    approved = _non_negative(_decimal(approved_quantity_pct))
    if trader == 0:
        return Decimal("0")
    return min(approved / trader, Decimal("1"))


def target_notional(
    *,
    equity: Decimal,
    mandate: Mandate,
    quantity_pct: Decimal | float,
    trader_quantity_pct: Decimal | float,
    approved_quantity_pct: Decimal | float,
) -> Decimal:
    """Compute deterministic target notional from mandate and PM scale."""

    quantity = _non_negative(_decimal(quantity_pct))
    return (
        _non_negative(equity)
        * _decimal(mandate.max_position_pct_equity)
        / ONE_HUNDRED
        * quantity
        / ONE_HUNDRED
        * pm_scale(trader_quantity_pct, approved_quantity_pct)
    )


def size_order(
    *,
    symbol: str,
    equity: Decimal,
    price: Decimal,
    mandate: Mandate,
    quantity_pct: Decimal | float,
    trader_quantity_pct: Decimal | float,
    approved_quantity_pct: Decimal | float,
) -> Decimal:
    """Return a deterministic share/coin quantity for the requested symbol."""

    if price <= 0:
        return Decimal("0")
    raw_qty = target_notional(
        equity=equity,
        mandate=mandate,
        quantity_pct=quantity_pct,
        trader_quantity_pct=trader_quantity_pct,
        approved_quantity_pct=approved_quantity_pct,
    ) / price
    if raw_qty <= 0:
        return Decimal("0")
    if symbol.upper().endswith("-USD"):
        return raw_qty.quantize(CRYPTO_QUANTUM, rounding=ROUND_FLOOR)
    return raw_qty.to_integral_value(rounding=ROUND_FLOOR)


def conviction_scaled_budget(conviction: Decimal | float | int) -> Decimal:
    """Return a deterministic max-loss budget fraction linear in conviction/100."""

    normalized = _non_negative(_decimal(conviction))
    return min(normalized, ONE_HUNDRED) / ONE_HUNDRED


def size_option_order(
    *,
    equity: Decimal,
    mandate: Mandate,
    conviction: Decimal | float | int,
    trader_quantity_pct: Decimal | float,
    approved_quantity_pct: Decimal | float,
    max_loss_per_1_spread: Decimal,
) -> int:
    """Return option contract count sized by max loss rather than notional."""

    if max_loss_per_1_spread <= 0:
        return 0
    scaled_budget = (
        _non_negative(equity)
        * pm_scale(trader_quantity_pct, approved_quantity_pct)
        * conviction_scaled_budget(conviction)
    )
    budget = min(_decimal(mandate.options.max_loss_per_position_usd), scaled_budget)
    contracts = (budget / max_loss_per_1_spread).to_integral_value(rounding=ROUND_FLOOR)
    return int(max(contracts, Decimal("0")))


def available_cash(portfolio: Portfolio, option_positions: Sequence[OptionPosition]) -> Decimal:
    """Return cash not already escrowed as option collateral."""

    escrowed = sum((_non_negative(position.collateral) for position in option_positions), Decimal("0"))
    return portfolio.cash - escrowed

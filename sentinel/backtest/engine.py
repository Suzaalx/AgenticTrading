"""Config-driven Sentinel backtesting engine."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import pandas as pd

from sentinel.backtest.metrics import SYNTHETIC_BACKTEST_NOTICE, compute_metrics
from sentinel.backtest.strategies import (
    BuyHoldStrategy,
    CoveredCallOverwriteStrategy,
    CSPWheelStrategy,
    RSIMeanRevertStrategy,
    SMACrossStrategy,
    Strategy,
)
from sentinel.backtest.strategies.base import BarContext
from sentinel.backtest.synthetic_chain import OptionRuleSignal, SyntheticChainProvider
from sentinel.config.settings import load_mandate, load_settings
from sentinel.core.bus import EventBus
from sentinel.core.events import BacktestProgress
from sentinel.core.ids import new_id
from sentinel.core.models import (
    BacktestResult,
    DataSnapshot,
    Fill,
    OptionChainSnapshot,
    OptionLeg,
    OptionPosition,
    OptionStrategyProposal,
    Order,
    Portfolio,
    Position,
    Signal,
)
from sentinel.data.indicators import compute_indicators
from sentinel.execution.portfolio import apply_option_fill
from sentinel.store.db import connect, run_migrations, sentinel_home

BacktestMode = Literal["rule", "agent"]
AgentDecision = Signal | OptionStrategyProposal | Mapping[str, Any] | Any


class AgentPipeline(Protocol):
    """Agent replay seam supplied by the future orchestrator."""

    def __call__(
        self,
        snapshot: DataSnapshot,
        *,
        option_chain: OptionChainSnapshot | None = None,
    ) -> Awaitable[AgentDecision]:
        """Return a decision from a point-in-time ``DataSnapshot``."""
        ...


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest engine configuration."""

    symbol: str
    mode: BacktestMode = "rule"
    start: date | None = None
    end: date | None = None
    strategy: str | Strategy = "sma_cross"
    strategy_params: Mapping[str, Any] | None = None
    cadence: str | int = "weekly"
    starting_cash: float | None = None
    commission_usd: float | None = None
    slippage_bps: float | None = None
    benchmark_symbol: str = "SPY"
    csv_path: Path | None = None
    data: pd.DataFrame | None = None
    benchmark_data: pd.DataFrame | None = None
    persist: bool = True
    db_path: Path | None = None
    bt_id: str | None = None
    bootstrap_iterations: int = 1000
    bootstrap_seed: int = 0
    options_enabled: bool | None = None
    options_universe: list[str] | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | BacktestConfig) -> BacktestConfig:
        if isinstance(raw, BacktestConfig):
            return raw
        values = dict(raw)
        if values.get("csv_path") is not None:
            values["csv_path"] = Path(values["csv_path"])
        if values.get("db_path") is not None:
            values["db_path"] = Path(values["db_path"])
        for key in ("start", "end"):
            if isinstance(values.get(key), str):
                values[key] = date.fromisoformat(cast(str, values[key]))
        return cls(**values)

    def persisted_dict(self) -> dict[str, Any]:
        strategy_name = self.strategy if isinstance(self.strategy, str) else type(self.strategy).__name__
        return {
            "symbol": self.symbol,
            "mode": self.mode,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "strategy": strategy_name,
            "strategy_params": dict(self.strategy_params or {}),
            "cadence": self.cadence,
            "starting_cash": self.starting_cash,
            "commission_usd": self.commission_usd,
            "slippage_bps": self.slippage_bps,
            "benchmark_symbol": self.benchmark_symbol,
            "csv_path": str(self.csv_path) if self.csv_path else None,
            "bootstrap_iterations": self.bootstrap_iterations,
            "bootstrap_seed": self.bootstrap_seed,
            "options_enabled": self.options_enabled,
            "options_universe": self.options_universe,
        }


def run_backtest(
    config: BacktestConfig | Mapping[str, Any],
    *,
    pipeline: AgentPipeline | Callable[..., Awaitable[AgentDecision]] | None = None,
    event_bus: EventBus | None = None,
) -> BacktestResult:
    """Run a backtest synchronously and return ``BacktestResult``."""

    return asyncio.run(run_backtest_async(config, pipeline=pipeline, event_bus=event_bus))


async def run_backtest_async(
    config: BacktestConfig | Mapping[str, Any],
    *,
    pipeline: AgentPipeline | Callable[..., Awaitable[AgentDecision]] | None = None,
    event_bus: EventBus | None = None,
) -> BacktestResult:
    """Run a rule or agent replay backtest."""

    cfg = BacktestConfig.from_mapping(config)
    settings = load_settings(Path.cwd())
    starting_cash = cfg.starting_cash or settings.execution.starting_cash_usd
    commission_usd = cfg.commission_usd
    if commission_usd is None:
        commission_usd = settings.execution.commission_usd
    slippage_bps = cfg.slippage_bps
    if slippage_bps is None:
        slippage_bps = float(settings.execution.slippage_bps)
    bt_id = cfg.bt_id or f"bt_{uuid4().hex}"
    raw_frame = _load_ohlcv(cfg)
    frame = compute_indicators(raw_frame)
    strategy = _strategy_from_config(cfg)
    result = await _simulate(
        cfg=cfg,
        bt_id=bt_id,
        frame=frame,
        raw_frame=raw_frame,
        strategy=strategy,
        pipeline=pipeline,
        event_bus=event_bus,
        starting_cash=float(starting_cash),
        commission_usd=float(commission_usd),
        slippage_bps=float(slippage_bps),
    )
    if cfg.persist:
        _persist_backtest(bt_id, cfg, result)
    return result


async def _simulate(
    *,
    cfg: BacktestConfig,
    bt_id: str,
    frame: pd.DataFrame,
    raw_frame: pd.DataFrame,
    strategy: Strategy,
    pipeline: AgentPipeline | Callable[..., Awaitable[AgentDecision]] | None,
    event_bus: EventBus | None,
    starting_cash: float,
    commission_usd: float,
    slippage_bps: float,
) -> BacktestResult:
    if frame.empty:
        msg = "backtest requires at least one OHLCV bar"
        raise ValueError(msg)
    if cfg.mode == "agent" and pipeline is None:
        msg = "agent mode requires an injected async pipeline callable"
        raise ValueError(msg)

    cash = starting_cash
    shares = 0.0
    avg_cost = 0.0
    entry_date: date | None = None
    entry_index: int | None = None
    pending_signal: Signal | None = None
    pending_option_signal: OptionRuleSignal | None = None
    pending_option_proposal: OptionStrategyProposal | None = None
    option_provider = SyntheticChainProvider(raw_frame, symbol=cfg.symbol)
    options_active = _options_active_for_config(cfg)
    option_positions: list[OptionPosition] = []
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    exposure_steps = 0
    turnover_notional = 0.0

    total_steps = len(frame)
    for index, (timestamp, raw_row) in enumerate(frame.iterrows()):
        row = cast(pd.Series, raw_row)
        ts = cast(pd.Timestamp, pd.Timestamp(cast(Any, timestamp)))
        open_price = float(cast(Any, row["open"]))
        close_price = float(cast(Any, row["close"]))
        if pending_option_signal is not None:
            option_result = _execute_option_signal(
                signal=pending_option_signal,
                symbol=cfg.symbol,
                ts=ts,
                open_price=open_price,
                cash=cash,
                shares=shares,
                avg_cost=avg_cost,
                option_positions=option_positions,
                provider=option_provider,
                commission_usd=commission_usd,
                run_id=bt_id,
            )
            cash = option_result.cash
            shares = option_result.shares
            avg_cost = option_result.avg_cost
            option_positions = option_result.option_positions
            turnover_notional += option_result.turnover_notional
            trades.extend(option_result.trades)
            pending_option_signal = None
        if pending_option_proposal is not None:
            option_result = _execute_option_proposal(
                proposal=pending_option_proposal,
                symbol=cfg.symbol,
                ts=ts,
                cash=cash,
                shares=shares,
                avg_cost=avg_cost,
                option_positions=option_positions,
                provider=option_provider,
                commission_usd=commission_usd,
                run_id=bt_id,
            )
            cash = option_result.cash
            shares = option_result.shares
            avg_cost = option_result.avg_cost
            option_positions = option_result.option_positions
            turnover_notional += option_result.turnover_notional
            trades.extend(option_result.trades)
            pending_option_proposal = None
        if pending_signal is not None:
            executed = _execute_signal(
                signal=pending_signal,
                symbol=cfg.symbol,
                ts=ts,
                bar_index=index,
                open_price=open_price,
                close_price=close_price,
                cash=cash,
                shares=shares,
                avg_cost=avg_cost,
                entry_date=entry_date,
                entry_index=entry_index,
                commission_usd=commission_usd,
                slippage_bps=slippage_bps,
            )
            cash = executed.cash
            shares = executed.shares
            avg_cost = executed.avg_cost
            entry_date = executed.entry_date
            entry_index = executed.entry_index
            turnover_notional += executed.turnover_notional
            trades.extend(executed.trades)
            pending_signal = None

        settlement = _settle_expired_options(
            symbol=cfg.symbol,
            ts=ts,
            close_price=close_price,
            cash=cash,
            shares=shares,
            avg_cost=avg_cost,
            option_positions=option_positions,
        )
        cash = settlement.cash
        shares = settlement.shares
        avg_cost = settlement.avg_cost
        option_positions = settlement.option_positions
        trades.extend(settlement.trades)

        option_marks = _option_marks(option_provider, ts, option_positions)
        option_value = _option_position_value(option_positions, option_marks)
        equity = cash + (shares * close_price) + option_value
        if shares > 0:
            exposure_steps += 1
        equity_curve.append(
            {
                "date": ts.date().isoformat(),
                "equity": equity,
                "cash": cash,
                "position_qty": shares,
                "option_position_count": len(option_positions),
                "option_value": option_value,
                "close": close_price,
                "pricing_notice": SYNTHETIC_BACKTEST_NOTICE if option_positions else None,
            }
        )

        if cfg.mode == "rule":
            _sync_option_strategy(strategy, bool(option_positions), shares)
            history = frame.iloc[: index + 1].copy()
            context = BarContext(
                symbol=cfg.symbol,
                timestamp=ts,
                bar=row,
                history=history,
                cash=cash,
                position_qty=shares,
                position_value=shares * close_price,
                equity=equity,
            )
            decision = strategy.on_bar(context)
            if isinstance(decision, OptionRuleSignal):
                pending_option_signal = decision if decision.action != "HOLD" else None
            else:
                pending_signal = decision
        elif _is_cadence_step(index, cfg.cadence):
            snapshot = _build_agent_snapshot(
                bt_id=bt_id,
                symbol=cfg.symbol,
                ts=ts,
                raw_history=raw_frame.iloc[: index + 1],
            )
            option_chain = (
                option_provider.chain_for(ts, run_id=snapshot.run_id)
                if options_active
                else None
            )
            decision = await _call_agent_pipeline(
                cast(Callable[..., Awaitable[AgentDecision]], pipeline),
                snapshot,
                option_chain=option_chain,
            )
            if isinstance(decision, OptionStrategyProposal):
                pending_option_proposal = decision if decision.action != "HOLD" else None
                pending_signal = None
            else:
                pending_signal = _decision_to_signal(decision)

        await _publish_progress(event_bus, bt_id, index + 1, total_steps)

    benchmark_data = cfg.benchmark_data
    if benchmark_data is None and cfg.benchmark_symbol.upper() == cfg.symbol.upper():
        benchmark_data = raw_frame
    return compute_metrics(
        equity_curve=equity_curve,
        trades=trades,
        starting_cash=starting_cash,
        benchmark_symbol=cfg.benchmark_symbol,
        benchmark_prices=benchmark_data,
        exposure_steps=exposure_steps,
        total_steps=total_steps,
        turnover_notional=turnover_notional,
        bootstrap_iterations=cfg.bootstrap_iterations,
        bootstrap_seed=cfg.bootstrap_seed,
    )


@dataclass(frozen=True)
class _ExecutionResult:
    cash: float
    shares: float
    avg_cost: float
    entry_date: date | None
    entry_index: int | None
    turnover_notional: float
    trades: list[dict[str, Any]]


@dataclass(frozen=True)
class _OptionExecutionResult:
    cash: float
    shares: float
    avg_cost: float
    option_positions: list[OptionPosition]
    turnover_notional: float
    trades: list[dict[str, Any]]


def _execute_signal(
    *,
    signal: Signal,
    symbol: str,
    ts: pd.Timestamp,
    bar_index: int,
    open_price: float,
    close_price: float,
    cash: float,
    shares: float,
    avg_cost: float,
    entry_date: date | None,
    entry_index: int | None,
    commission_usd: float,
    slippage_bps: float,
) -> _ExecutionResult:
    action = signal.action.upper()
    size = _bounded_size(signal.size)
    trades: list[dict[str, Any]] = []
    turnover_notional = 0.0
    if action == "BUY":
        fill_price = open_price * (1.0 + (slippage_bps / 10_000.0))
        equity_at_open = cash + (shares * open_price)
        target_value = equity_at_open * size
        current_value = shares * open_price
        buy_notional = max(0.0, target_value - current_value)
        available_notional = max(0.0, cash - commission_usd)
        buy_notional = min(buy_notional, available_notional)
        qty = buy_notional / fill_price if fill_price > 0 else 0.0
        if qty > 1e-12:
            previous_cost = shares * avg_cost
            cash -= (qty * fill_price) + commission_usd
            shares += qty
            avg_cost = (previous_cost + (qty * fill_price)) / shares
            if entry_date is None:
                entry_date = ts.date()
                entry_index = bar_index
            turnover_notional = qty * fill_price
    elif action == "SELL" and shares > 0:
        fill_price = open_price * (1.0 - (slippage_bps / 10_000.0))
        qty = min(shares, shares * size)
        if qty > 1e-12:
            notional = qty * fill_price
            cash += notional - commission_usd
            pnl = ((fill_price - avg_cost) * qty) - commission_usd
            turnover_notional = notional
            trades.append(
                {
                    "symbol": symbol,
                    "entry_date": entry_date.isoformat() if entry_date else None,
                    "entry_price": avg_cost,
                    "exit_date": ts.date().isoformat(),
                    "exit_price": fill_price,
                    "qty": qty,
                    "pnl": pnl,
                    "return": (fill_price / avg_cost) - 1.0 if avg_cost else 0.0,
                    "bars_held": (bar_index - entry_index) if entry_index is not None else None,
                }
            )
            shares -= qty
            if shares <= 1e-12:
                shares = 0.0
                avg_cost = 0.0
                entry_date = None
                entry_index = None
    return _ExecutionResult(
        cash=cash,
        shares=shares,
        avg_cost=avg_cost,
        entry_date=entry_date,
        entry_index=entry_index,
        turnover_notional=turnover_notional,
        trades=trades,
    )


def _execute_option_signal(
    *,
    signal: OptionRuleSignal,
    symbol: str,
    ts: pd.Timestamp,
    open_price: float,
    cash: float,
    shares: float,
    avg_cost: float,
    option_positions: list[OptionPosition],
    provider: SyntheticChainProvider,
    commission_usd: float,
    run_id: str,
) -> _OptionExecutionResult:
    if signal.action == "HOLD":
        return _OptionExecutionResult(cash, shares, avg_cost, option_positions, 0.0, [])

    trades: list[dict[str, Any]] = []
    turnover = 0.0
    required_shares = 100 * signal.contracts
    if signal.action == "OPEN_COVERED_CALL" and shares < required_shares:
        qty = required_shares - shares
        notional = qty * open_price
        if cash < notional + commission_usd:
            return _OptionExecutionResult(cash, shares, avg_cost, option_positions, 0.0, [])
        previous_cost = shares * avg_cost
        cash -= notional + commission_usd
        shares += qty
        avg_cost = (previous_cost + notional) / shares
        turnover += notional

    chain = provider.chain_for(ts, price="open", run_id=f"{run_id}:{ts.date().isoformat()}:open")
    kind = "call" if signal.action == "OPEN_COVERED_CALL" else "put"
    quote = _select_delta_quote(chain, kind=kind, target_delta=signal.target_delta, target_dte=signal.target_dte)
    if quote is None:
        return _OptionExecutionResult(cash, shares, avg_cost, option_positions, turnover, trades)

    leg = OptionLeg(
        contract=quote.contract,
        side="sell",
        contracts=signal.contracts,
        limit_price=quote.bid,
    )
    premium = -(quote.bid * Decimal(signal.contracts) * Decimal(quote.contract.multiplier))
    portfolio = _portfolio_from_state(cash, shares, avg_cost, symbol, ts.to_pydatetime().replace(tzinfo=UTC), run_id)
    order = Order(
        order_id=new_id(),
        run_id=run_id,
        symbol=symbol,
        side="sell",
        qty=Decimal(signal.contracts),
        type="net_credit",
        reason="agent_decision",
        created_at=ts.to_pydatetime().replace(tzinfo=UTC),
        asset_type="option",
        legs=[leg],
        strategy="covered_call" if kind == "call" else "cash_secured_put",
    )
    fill = Fill(
        order_id=order.order_id,
        price=premium,
        qty=Decimal("1"),
        ts=order.created_at,
        slippage_usd=Decimal("0"),
        commission_usd=Decimal(str(commission_usd)),
    )
    try:
        updated, updated_options, _realized = apply_option_fill(portfolio, option_positions, order, fill)
    except ValueError:
        return _OptionExecutionResult(cash, shares, avg_cost, option_positions, turnover, trades)

    credit = float(-premium)
    trades.append(
        {
            "symbol": quote.contract.contract_symbol,
            "underlying": symbol,
            "strategy": signal.strategy or ("covered_call_overwrite" if kind == "call" else "csp_wheel"),
            "option_strategy": order.strategy,
            "entry_date": ts.date().isoformat(),
            "entry_price": float(quote.bid),
            "qty": signal.contracts,
            "pnl": credit - commission_usd,
            "premium_captured": credit - commission_usd,
            "assignments": 0,
            "pricing_source": chain.pricing_source,
            "pricing_notice": SYNTHETIC_BACKTEST_NOTICE,
        }
    )
    return _OptionExecutionResult(
        cash=float(updated.cash),
        shares=shares,
        avg_cost=avg_cost,
        option_positions=updated_options,
        turnover_notional=turnover + credit,
        trades=trades,
    )


def _execute_option_proposal(
    *,
    proposal: OptionStrategyProposal,
    symbol: str,
    ts: pd.Timestamp,
    cash: float,
    shares: float,
    avg_cost: float,
    option_positions: list[OptionPosition],
    provider: SyntheticChainProvider,
    commission_usd: float,
    run_id: str,
) -> _OptionExecutionResult:
    if proposal.action != "OPEN":
        return _option_hold_result(
            cash,
            shares,
            avg_cost,
            option_positions,
            ts,
            symbol,
            proposal.strategy,
            f"unmapped option proposal action: {proposal.action}",
        )
    if not _is_mappable_option_proposal(proposal):
        return _option_hold_result(
            cash,
            shares,
            avg_cost,
            option_positions,
            ts,
            symbol,
            proposal.strategy,
            f"unmapped option proposal structure: {proposal.strategy}",
        )

    chain = provider.chain_for(ts, price="open", run_id=f"{run_id}:{ts.date().isoformat()}:open")
    quotes = {quote.contract.contract_symbol: quote for quote in chain.quotes}
    repriced_legs: list[OptionLeg] = []
    for leg in proposal.legs:
        quote = quotes.get(leg.contract.contract_symbol)
        if quote is None:
            return _option_hold_result(
                cash,
                shares,
                avg_cost,
                option_positions,
                ts,
                symbol,
                proposal.strategy,
                f"unmapped option proposal: missing synthetic open quote for {leg.contract.contract_symbol}",
            )
        price = quote.ask if leg.side == "buy" else quote.bid
        repriced_legs.append(
            leg.model_copy(update={"contract": quote.contract, "limit_price": price})
        )

    turnover = 0.0
    if proposal.strategy == "covered_call":
        contracts = sum(leg.contracts for leg in repriced_legs if leg.side == "sell")
        required_shares = 100 * contracts
        if shares < required_shares:
            open_price = float(chain.spot)
            qty = required_shares - shares
            notional = qty * open_price
            if cash < notional + commission_usd:
                return _option_hold_result(
                    cash,
                    shares,
                    avg_cost,
                    option_positions,
                    ts,
                    symbol,
                    proposal.strategy,
                    "covered-call proposal held: insufficient cash to cover underlying shares",
                )
            previous_cost = shares * avg_cost
            cash -= notional + commission_usd
            shares += qty
            avg_cost = (previous_cost + notional) / shares
            turnover += notional

    premium = sum(
        (
            (Decimal("1") if leg.side == "buy" else Decimal("-1"))
            * (leg.limit_price or Decimal("0"))
            * Decimal(leg.contracts)
            * Decimal(leg.contract.multiplier)
            for leg in repriced_legs
        ),
        Decimal("0"),
    )
    portfolio = _portfolio_from_state(cash, shares, avg_cost, symbol, ts.to_pydatetime().replace(tzinfo=UTC), run_id)
    order = Order(
        order_id=new_id(),
        run_id=run_id,
        symbol=symbol,
        side="buy",
        qty=Decimal(max((leg.contracts for leg in repriced_legs), default=1)),
        type="net_debit" if premium >= 0 else "net_credit",
        reason="agent_decision",
        created_at=ts.to_pydatetime().replace(tzinfo=UTC),
        asset_type="option",
        legs=repriced_legs,
        strategy=proposal.strategy,
    )
    fill = Fill(
        order_id=order.order_id,
        price=premium,
        qty=Decimal("1"),
        ts=order.created_at,
        slippage_usd=Decimal("0"),
        commission_usd=Decimal(str(commission_usd)),
    )
    try:
        updated, updated_options, _realized = apply_option_fill(portfolio, option_positions, order, fill)
    except ValueError as exc:
        return _option_hold_result(
            cash,
            shares,
            avg_cost,
            option_positions,
            ts,
            symbol,
            proposal.strategy,
            f"option proposal held: {exc}",
        )

    turnover += abs(float(premium))
    trades = [
        {
            "symbol": symbol,
            "underlying": symbol,
            "strategy": proposal.strategy,
            "option_strategy": proposal.strategy,
            "entry_date": ts.date().isoformat(),
            "entry_price": float(premium),
            "qty": int(order.qty),
            "pnl": -commission_usd,
            "premium_captured": max(0.0, float(-premium) - commission_usd),
            "assignments": 0,
            "pricing_source": chain.pricing_source,
            "pricing_notice": SYNTHETIC_BACKTEST_NOTICE,
        }
    ]
    return _OptionExecutionResult(
        cash=float(updated.cash),
        shares=shares,
        avg_cost=avg_cost,
        option_positions=updated_options,
        turnover_notional=turnover,
        trades=trades,
    )


def _is_mappable_option_proposal(proposal: OptionStrategyProposal) -> bool:
    if not proposal.legs:
        return False
    if proposal.strategy in {"long_call", "long_put", "covered_call", "cash_secured_put"}:
        return len(proposal.legs) == 1
    if proposal.strategy in {"bull_call_spread", "bear_put_spread", "bull_put_spread", "bear_call_spread"}:
        return len(proposal.legs) == 2
    return False


def _option_hold_result(
    cash: float,
    shares: float,
    avg_cost: float,
    option_positions: list[OptionPosition],
    ts: pd.Timestamp,
    symbol: str,
    strategy: str,
    note: str,
) -> _OptionExecutionResult:
    return _OptionExecutionResult(
        cash=cash,
        shares=shares,
        avg_cost=avg_cost,
        option_positions=option_positions,
        turnover_notional=0.0,
        trades=[
            {
                "symbol": symbol,
                "underlying": symbol,
                "strategy": strategy,
                "option_strategy": strategy,
                "entry_date": ts.date().isoformat(),
                "action": "HOLD",
                "pnl": 0.0,
                "premium_captured": 0.0,
                "assignments": 0,
                "pricing_source": "synthetic_bsm",
                "pricing_notice": SYNTHETIC_BACKTEST_NOTICE,
                "note": note,
            }
        ],
    )


@dataclass(frozen=True)
class _OptionSettlementResult:
    cash: float
    shares: float
    avg_cost: float
    option_positions: list[OptionPosition]
    trades: list[dict[str, Any]]


def _settle_expired_options(
    *,
    symbol: str,
    ts: pd.Timestamp,
    close_price: float,
    cash: float,
    shares: float,
    avg_cost: float,
    option_positions: list[OptionPosition],
) -> _OptionSettlementResult:
    remaining: list[OptionPosition] = []
    trades: list[dict[str, Any]] = []
    for position in option_positions:
        if position.expiry > ts.date():
            remaining.append(position)
            continue
        assignments = 0
        for leg in position.legs:
            strike = float(leg.contract.strike)
            qty = leg.contracts * leg.contract.multiplier
            if leg.side != "sell":
                continue
            if leg.contract.kind == "call" and close_price > strike:
                assigned_qty = min(shares, float(qty))
                cash += assigned_qty * strike
                shares -= assigned_qty
                assignments += 1
                if shares <= 1e-12:
                    shares = 0.0
                    avg_cost = 0.0
            elif leg.contract.kind == "put" and close_price < strike:
                notional = float(qty) * strike
                previous_cost = shares * avg_cost
                cash -= notional
                shares += float(qty)
                avg_cost = (previous_cost + notional) / shares
                assignments += 1
        trades.append(
            {
                "symbol": symbol,
                "strategy": str(position.strategy),
                "exit_date": ts.date().isoformat(),
                "pnl": 0.0,
                "premium_captured": 0.0,
                "assignments": assignments,
                "pricing_source": "synthetic_bsm",
                "pricing_notice": SYNTHETIC_BACKTEST_NOTICE,
            }
        )
    return _OptionSettlementResult(cash, shares, avg_cost, remaining, trades)


def _portfolio_from_state(
    cash: float,
    shares: float,
    avg_cost: float,
    symbol: str,
    opened_at: datetime,
    run_id: str,
) -> Portfolio:
    positions: list[Position] = []
    if shares > 1e-12:
        positions.append(
            Position(
                symbol=symbol,
                qty=Decimal(str(shares)),
                avg_cost=Decimal(str(avg_cost)),
                stop_loss_pct=None,
                take_profit_pct=None,
                opened_at=opened_at,
                horizon_days=None,
                source_run_id=run_id,
            )
        )
    return Portfolio(cash=Decimal(str(cash)), positions=positions)


def _select_delta_quote(
    chain: OptionChainSnapshot,
    *,
    kind: str,
    target_delta: float,
    target_dte: int,
) -> Any | None:
    quotes = [
        quote
        for quote in chain.quotes
        if quote.contract.kind == kind and quote.delta is not None and quote.bid > Decimal("0")
    ]
    if not quotes:
        return None
    as_of = chain.as_of.date()
    signed_target = target_delta if kind == "call" else -target_delta
    return min(
        quotes,
        key=lambda quote: (
            abs((quote.contract.expiry - as_of).days - target_dte),
            abs(float(cast(float, quote.delta)) - signed_target),
        ),
    )


def _option_marks(
    provider: SyntheticChainProvider,
    ts: pd.Timestamp,
    option_positions: list[OptionPosition],
) -> dict[str, float]:
    if not option_positions:
        return {}
    chain = provider.chain_for(ts, price="close")
    return {
        quote.contract.contract_symbol: (float(quote.bid) + float(quote.ask)) / 2.0
        for quote in chain.quotes
    }


def _option_position_value(
    option_positions: list[OptionPosition],
    marks: dict[str, float],
) -> float:
    total = 0.0
    for position in option_positions:
        for leg in position.legs:
            mark = marks.get(leg.contract.contract_symbol)
            if mark is None:
                mark = float(leg.limit_price or Decimal("0"))
            sign = 1.0 if leg.side == "buy" else -1.0
            total += sign * mark * leg.contracts * leg.contract.multiplier
    return total


def _sync_option_strategy(strategy: Strategy, has_open_options: bool, shares: float) -> None:
    sync = getattr(strategy, "sync_option_state", None)
    if callable(sync):
        sync(has_open_options, shares)


async def _call_agent_pipeline(
    pipeline: Callable[..., Awaitable[AgentDecision]],
    snapshot: DataSnapshot,
    *,
    option_chain: OptionChainSnapshot | None,
) -> AgentDecision:
    if option_chain is None:
        return await pipeline(snapshot)
    try:
        return await pipeline(snapshot, option_chain=option_chain)
    except TypeError:
        return await pipeline(snapshot)


def _options_active_for_config(cfg: BacktestConfig) -> bool:
    if cfg.options_enabled is False:
        return False
    symbol = cfg.symbol.upper()
    if cfg.options_universe is not None:
        in_universe = symbol in {item.upper() for item in cfg.options_universe}
        return bool(cfg.options_enabled) and in_universe
    mandate = load_mandate(Path.cwd())
    in_universe = symbol in {item.upper() for item in mandate.options.underlying_universe}
    enabled = cfg.options_enabled if cfg.options_enabled is not None else mandate.options.enabled
    return bool(enabled and in_universe)


def _load_ohlcv(cfg: BacktestConfig) -> pd.DataFrame:
    if cfg.data is not None:
        return _normalize_frame(cfg.data)
    if cfg.csv_path is not None:
        return _normalize_frame(_read_csv(cfg.csv_path))
    if cfg.start is None or cfg.end is None:
        msg = "start and end are required when no data frame or csv_path is supplied"
        raise ValueError(msg)
    from sentinel.data.router import DataRouter

    router = DataRouter(settings=load_settings(Path.cwd()))
    return _normalize_frame(router.get_ohlcv(cfg.symbol, cfg.start, cfg.end))


def _read_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    date_column = "date" if "date" in {str(col).lower() for col in frame.columns} else frame.columns[0]
    if date_column != frame.columns[0]:
        for column in frame.columns:
            if str(column).lower() == "date":
                date_column = column
                break
    frame[date_column] = pd.to_datetime(frame[date_column])
    return frame.set_index(date_column)


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(column).lower() for column in out.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(out.columns)
    if missing:
        msg = f"OHLCV frame missing columns: {sorted(missing)}"
        raise ValueError(msg)
    out.index = [
        cast(pd.Timestamp, pd.Timestamp(cast(Any, value))).normalize() for value in out.index
    ]
    out = out.sort_index()
    return cast(pd.DataFrame, out.loc[~out.index.duplicated(keep="last")])


def _strategy_from_config(cfg: BacktestConfig) -> Strategy:
    if not isinstance(cfg.strategy, str):
        return cfg.strategy
    params = dict(cfg.strategy_params or {})
    name = cfg.strategy.lower()
    if name == "sma_cross":
        return SMACrossStrategy(**params)
    if name == "rsi_meanrevert":
        return RSIMeanRevertStrategy(**params)
    if name == "buy_hold":
        return BuyHoldStrategy(**params)
    if name == "covered_call_overwrite":
        return cast(Strategy, CoveredCallOverwriteStrategy(**params))
    if name == "csp_wheel":
        return cast(Strategy, CSPWheelStrategy(**params))
    msg = f"unknown strategy: {cfg.strategy}"
    raise ValueError(msg)


def _bounded_size(size: float | None) -> float:
    if size is None:
        return 1.0
    return min(1.0, max(0.0, float(size)))


def _is_cadence_step(index: int, cadence: str | int) -> bool:
    if isinstance(cadence, int):
        return cadence > 0 and index % cadence == 0
    normalized = cadence.lower()
    if normalized == "daily":
        return True
    if normalized == "weekly":
        return index % 5 == 0
    if normalized == "monthly":
        return index % 21 == 0
    msg = "cadence must be daily, weekly, monthly, or a positive integer"
    raise ValueError(msg)


def _build_agent_snapshot(
    *,
    bt_id: str,
    symbol: str,
    ts: pd.Timestamp,
    raw_history: pd.DataFrame,
) -> DataSnapshot:
    snapshot_root = sentinel_home() / "backtests" / bt_id / ts.strftime("%Y%m%d")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    ohlcv_path = snapshot_root / "ohlcv.parquet"
    indicators_path = snapshot_root / "indicators.parquet"
    history = _normalize_frame(raw_history)
    indicators = compute_indicators(history)
    history.to_parquet(ohlcv_path)
    indicators.to_parquet(indicators_path)
    return DataSnapshot(
        run_id=f"{bt_id}:{ts.date().isoformat()}",
        symbol=symbol,
        as_of=ts.to_pydatetime().replace(tzinfo=UTC),
        ohlcv_path=str(ohlcv_path),
        indicators_path=str(indicators_path),
        news=[],
        fundamentals=None,
        providers_used={"ohlcv": "backtest_frame", "indicators": "computed"},
    )


def _decision_to_signal(decision: AgentDecision) -> Signal:
    if isinstance(decision, Signal):
        return decision
    if isinstance(decision, Mapping):
        action = str(decision.get("action", "HOLD")).upper()
        size = decision.get("size")
        return Signal(action=cast(Literal["BUY", "SELL", "HOLD"], action), size=size)
    action = str(getattr(decision, "action", "HOLD")).upper()
    size = getattr(decision, "size", None)
    return Signal(action=cast(Literal["BUY", "SELL", "HOLD"], action), size=size)


async def _publish_progress(
    event_bus: EventBus | None, bt_id: str, completed_steps: int, total_steps: int
) -> None:
    if event_bus is None:
        return
    await event_bus.publish(
        BacktestProgress(
            bt_id=bt_id,
            completed_steps=completed_steps,
            total_steps=total_steps,
            message=f"{completed_steps}/{total_steps} bars replayed",
        )
    )


def _persist_backtest(bt_id: str, cfg: BacktestConfig, result: BacktestResult) -> None:
    conn = connect(cfg.db_path)
    try:
        run_migrations(conn)
        metrics_json = json.dumps(_result_metrics_dict(result), allow_nan=True)
        equity_json = json.dumps(result.equity_curve, allow_nan=True, default=str)
        conn.execute(
            """INSERT OR REPLACE INTO backtests
            (bt_id, config_json, metrics_json, equity_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                bt_id,
                json.dumps(cfg.persisted_dict(), default=str),
                metrics_json,
                equity_json,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _result_metrics_dict(result: BacktestResult) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    for key in ("premium_captured", "assignments", "win_rate_by_strategy", "pricing_notice"):
        if key in result.__dict__:
            payload[key] = result.__dict__[key]
    return payload

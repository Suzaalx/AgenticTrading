# Sentinel

**Sentinel** is a personal, terminal-first, single-user agentic trading system for research, paper trading, staged live trading, and backtesting. It runs a multi-agent pipeline, records every decision, routes approved orders through deterministic risk gates, and keeps paper mode as the default fallback.

> **Research use only. Not financial advice.** Sentinel is software for experimentation and education, not a recommendation engine. If you enable live trading, **you are giving software authority over real money in your own Robinhood account; losses are yours; official Robinhood rails only; start tiny.**

## Agent pipeline

1. Data snapshots are pinned with provider provenance.
2. Market, fundamentals, news, sentiment, and options analysts produce typed reports.
3. Bull and bear researchers debate the evidence; a research manager judges the thesis.
4. The trader proposes a stock, crypto, or defined-risk options action.
5. Risk debaters and the portfolio manager review it.
6. Deterministic mandate and live-risk gates make the final allow/veto decision.
7. Execution, reconciliation, journal scoring, and reflection are persisted and surfaced in the TUI.

## Graduation ladder

Sentinel does not have one global “go live” switch. Each asset class graduates independently:

| Stage | Scope | Rail | Gate |
|---|---|---|---|
| 0 paper | stocks, crypto, options | PaperBroker | default, always available |
| 1 live crypto | BTC-USD, ETH-USD | official Robinhood Crypto Trading API | paper track record, env keys, live mandate, typed confirmation |
| 2 live equities | mandate stock universe | Robinhood Agentic Trading MCP | paper equity history, linked/funded agentic account, typed confirmation |
| 3 live options | defined-risk menu | Robinhood Agentic Trading MCP when options support exists | Stage 2 seasoning, paper option closes, capability probe, typed confirmation |

## Options

Options are first-class but **defined-risk only**. The supported menu is: long call, long put, covered call, cash-secured put, bull call spread, bear put spread, bull put spread, bear call spread, long straddle, and long strangle. Local deterministic math computes BSM/CRR prices, Greeks, Brent-style IV inversion, max loss, collateral, and candidate facts before any LLM sees a strategy.

Backtests can use synthetic option chains. These artifacts are labeled `pricing_source = "synthetic_bsm"` and mean **synthetic pricing — not indicative of live fills**.

## Quickstart

```powershell
uv run pytest
sentinel
sentinel doctor
sentinel run NVDA
sentinel backtest --help
```

Useful commands:

```powershell
sentinel doctor --live
sentinel reconcile --adopt-broker
sentinel kill on --flatten
sentinel demote crypto
```

## Config and secrets

- `config.toml` controls providers, options modeling, backtests, live runtime intervals, and execution rails.
- `mandate.toml` controls deterministic risk limits, options knobs under `[mandate.options]`, and live stage flags under `[mandate.live]`.
- Secrets stay in environment variables or `.env`, never in TOML: LLM keys, market-data keys, Robinhood Crypto API key/private key, and Agentic MCP endpoint credentials.
- Live allocation must be capped twice: by Robinhood funding limits and by Sentinel’s `max_account_allocation_usd`. Sentinel’s live mandate should be less than or equal to the Robinhood funding cap.

See `docs/OPTIONS.md` for option math and lifecycle details, and `docs/LIVE.md` for live prerequisites, reconciliation, and incident response.

# Phase 0 Decisions

- DDL migrations use `CREATE TABLE IF NOT EXISTS`/`CREATE VIRTUAL TABLE IF NOT EXISTS` for idempotent startup while preserving the SPEC §15 schema columns.
- Configuration loading reads `config.toml` explicitly, overlays `.env`/process environment, and validates with pydantic-settings models so tests and downstream tools can override values without network access.
- Phase 0 TUI panes are placeholders only; stable tab IDs are `dashboard`, `run`, `portfolio`, `history`, `backtest`, `memory`, and `logs`.
- Phase 3 orchestration uses a hand-rolled async state machine instead of LangGraph. SPEC §1.3/§4 permit keeping orchestration dependency-light, and this avoids heavy LangGraph/LangChain dependencies on ARM64-emulated Python while preserving checkpoint/resume semantics.

# WS10 Product Decisions

- **Official rails only:** Sentinel uses only Robinhood Crypto Trading API and Robinhood Agentic Trading MCP for live Robinhood execution. Unofficial/reverse-engineered APIs are rejected because they create ToS, account-lockout, authentication, and reliability risk.
- **BSM+CRR local Greeks:** Option pricing and Greeks are computed locally with deterministic Black-Scholes-Merton and Cox-Ross-Rubinstein code; LLM agents never price options or compute Greeks.
- **Brent IV inversion:** Implied volatility is solved locally with bracketed Brent/bisection-style inversion after no-arbitrage checks, keeping IV deterministic and testable.
- **Defined-risk only:** Options remain finite-risk only; the mandate validator rejects `defined_risk_only=false`, and naked shorts are structurally vetoed by the gate.
- **Force-close before expiry:** Options are force-closed at or before configured `force_close_dte` to reduce assignment/expiry surprises; expiry settlement is deterministic fallback handling.
- **Synthetic backtest pricing:** Synthetic option backtests use `pricing_source="synthetic_bsm"` and must be labeled as synthetic pricing, not indicative of live fills.
- **Broker as source of truth:** In live mode Robinhood is authoritative; Sentinel mirrors broker state and halts new live risk on reconciliation mismatch until reviewed or adopted.
- **Limit orders only by default:** Live mode defaults to limit/marketable-limit orders through the live mandate; market orders are vetoed except explicit kill-flatten handling.

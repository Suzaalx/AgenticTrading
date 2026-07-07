# Phase 0 Decisions

- DDL migrations use `CREATE TABLE IF NOT EXISTS`/`CREATE VIRTUAL TABLE IF NOT EXISTS` for idempotent startup while preserving the SPEC §15 schema columns.
- Configuration loading reads `config.toml` explicitly, overlays `.env`/process environment, and validates with pydantic-settings models so tests and downstream tools can override values without network access.
- Phase 0 TUI panes are placeholders only; stable tab IDs are `dashboard`, `run`, `portfolio`, `history`, `backtest`, `memory`, and `logs`.
- Phase 3 orchestration uses a hand-rolled async state machine instead of LangGraph. SPEC §1.3/§4 permit keeping orchestration dependency-light, and this avoids heavy LangGraph/LangChain dependencies on ARM64-emulated Python while preserving checkpoint/resume semantics.

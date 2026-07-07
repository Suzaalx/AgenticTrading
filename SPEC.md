# Sentinel — Personal Agentic Trading System

**Full Specification & Implementation Plan, v1.0** (2026-07-06)

This document is a complete, self-contained build specification. It is written to be handed to
an autonomous coding agent that will implement the system from scratch. Every section is
normative unless marked *(informative)*. Where the spec says MUST/SHOULD/MAY, follow RFC-2119
semantics.

---

## 0. How to use this document (instructions to the builder agent)

1. Implement in the **phase order of §19**. Each phase has acceptance criteria; do not start
   phase N+1 until phase N's criteria pass.
2. All code in **Python 3.12+**, fully typed, `pydantic` v2 models for every data structure
   that crosses a module boundary.
3. The system MUST run entirely in **paper/simulated mode by default**. Live trading is out of
   scope for v1 (the broker abstraction exists, but only the paper connector is implemented).
4. When a detail is unspecified, prefer the simplest implementation that satisfies the
   acceptance criteria, and record the decision in `docs/DECISIONS.md`.
5. Never hardcode API keys. All secrets come from `.env` (see §14).

---

## 1. Research foundations *(informative)*

This design synthesizes two open-source systems:

### 1.1 TauricResearch/TradingAgents (arXiv 2412.20138)

A LangGraph-based multi-agent framework that mirrors a trading firm:

- **Analyst team** (fundamentals, sentiment, news, technicals) produces structured reports
  from market data.
- **Researcher team**: a Bull and a Bear researcher debate the analyst reports over N rounds;
  a Research Manager judges the debate and issues an investment plan.
- **Trader** converts the plan into a concrete proposal (BUY/SELL/HOLD).
- **Risk team**: aggressive/neutral/conservative debaters stress-test the proposal; a
  **Portfolio Manager** gives the final approve/reject and the order executes on a simulated
  exchange.
- **Communication protocol**: agents exchange *structured reports*, not free chat — this
  reduces context bloat and "telephone game" degradation.
- **Two LLM tiers**: a `deep_think` model for debate/judgment and a cheap `quick_think` model
  for data summarization — the dominant cost lever.
- **Memory/reflection**: completed runs are appended to a decision log with realized returns
  vs. a benchmark; lessons are injected into future Portfolio Manager prompts.
- **Key empirical finding**: adversarial (bull/bear) debate materially outperforms
  single-agent decisions; the contrarian viewpoint reduces overconfidence; the risk debate
  reduces drawdowns.
- Config surface worth copying: `max_debate_rounds`, `max_risk_discuss_rounds`,
  `temperature=0` for reproducibility, checkpoint/resume of the graph, decision log path.

### 1.2 HKUDS/Vibe-Trading

A production-grade AI research workspace. The parts we adopt:

- **Safety architecture**: a user-committed **Mandate** (symbol universe, max order size,
  exposure caps, daily loss cap) enforced *in code* before any order; a **filesystem kill
  switch** that halts all trading instantly; an append-only **audit ledger**.
- **Data layer**: a unified loader registry with per-market **fallback chains** ordered by
  reliability (e.g. US: yahoo → stooq → tiingo → alphavantage → local), and OHLC sanity
  checks (drop bars with high < low, non-positive prices) at the loader boundary.
- **Backtesting**: config-driven engine with validation, and Monte Carlo / bootstrap
  robustness checks; lookahead-bias sentinel tests.
- **Session persistence**: JSONL event streams with fsync-on-append and corruption-tolerant
  reads; token/cost usage persisted per run (`llm_usage.json`).
- **Provider resilience**: retry on stream failure, graceful degradation instead of failing
  the whole run.
- Their TUI (prompt_toolkit + rich) validates that a terminal-first experience works; we go
  further with a full **Textual** dashboard app (§13).

### 1.3 What we deliberately do NOT copy

- Vibe-Trading's 10-broker matrix, 16 IM adapters, 456-factor Alpha Zoo, MCP server, web UI —
  massive scope, irrelevant to a personal agent. We keep *one* broker abstraction with a
  paper connector and a clean seam for adding Alpaca later.
- TradingAgents' provider zoo — we support OpenAI + Anthropic + any OpenAI-compatible base
  URL, nothing else.
- LangChain. We use **LangGraph only** for orchestration (it's independently useful:
  checkpointing, typed state) with direct provider SDK calls, or — builder's choice — a
  hand-rolled state machine if LangGraph friction is high. Record the choice in DECISIONS.md.

---

## 2. Product definition

### 2.1 What this is

**Sentinel** is a personal, single-user, terminal-first agentic trading system that:

1. On demand or on a schedule, runs a **multi-agent analysis pipeline** on a ticker and
   produces a fully-reasoned trade decision (BUY/SELL/HOLD + size + rationale).
2. Executes approved decisions on a **paper portfolio** (internal simulated broker).
3. **Backtests** the agent pipeline or rule-based strategies over historical windows.
4. Learns via a **reflection loop**: realized outcomes are scored and fed back into future
   decisions.
5. Exposes everything through a **terminal GUI (TUI)**: portfolio, live agent activity,
   debate transcripts, decision history, risk state, charts, logs, and cost tracking.

### 2.2 Goals

- G1: A complete decision run (one ticker, one date) costs **< $0.50** with default models
  and finishes in **< 4 minutes**.
- G2: Every decision is **fully auditable**: every agent's input, output, model, token
  counts, and the final order are persisted and browsable in the TUI.
- G3: **Zero orders can bypass the risk gate.** Mandate checks are pure functions in code,
  not LLM judgments.
- G4: The system is **resumable**: kill the process mid-run, restart, and continue.
- G5: Runs are **reproducible** given the same data snapshot and temperature 0.

### 2.3 Non-goals (v1)

- No live-money trading (paper only; the connector interface anticipates it).
- No options/futures/forex — **US equities + major crypto (BTC-USD, ETH-USD)** only.
- No web UI, no mobile, no multi-user, no IM integrations.
- No high-frequency anything. Decision cadence is daily (or on-demand intraday snapshots).
- Not investment advice; README must carry a research-use disclaimer.

### 2.4 Guiding principles

- **Determinism at the edges, intelligence in the middle.** Data fetching, risk checks,
  order execution, and accounting are deterministic code. LLMs only analyze and argue.
- **Structured reports over chat.** Agents communicate via typed pydantic reports.
- **Cheap by default.** quick-tier model everywhere except debate judgment and final gate.
- **Everything is an event.** All state changes flow through one event bus; the TUI is just
  a subscriber (§13.7); the session log is just another subscriber.

---

## 3. System architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                            TUI (Textual app)                         │
│   Dashboard │ Run Monitor │ Portfolio │ History │ Backtest │ Logs    │
└───────▲──────────────────────────────────────────────────────────────┘
        │ subscribes (EventBus) + issues commands (CommandService)
┌───────┴──────────────────────────────────────────────────────────────┐
│                         Orchestrator (LangGraph)                     │
│  AnalystStage → ResearchDebate → Trader → RiskDebate → PM Gate       │
│           (checkpointed state machine, per-run)                      │
└───┬──────────────┬───────────────┬─────────────────┬─────────────────┘
    │              │               │                 │
┌───▼────┐   ┌─────▼─────┐   ┌─────▼─────┐   ┌───────▼────────┐
│ Data   │   │ LLM       │   │ Risk      │   │ Execution      │
│ Layer  │   │ Gateway   │   │ Engine    │   │ Layer          │
│(loaders│   │(providers,│   │(mandates, │   │(PaperBroker,   │
│ +cache)│   │ retry,    │   │ kill      │   │ Portfolio,     │
│        │   │ cost      │   │ switch,   │   │ order ledger)  │
│        │   │ metering) │   │ sizing)   │   │                │
└───┬────┘   └───────────┘   └───────────┘   └───────┬────────┘
    │                                                │
┌───▼────────────────────────────────────────────────▼────────┐
│                    Storage (SQLite + files)                  │
│  runs, reports, orders, positions, fills, memory, costs     │
└──────────────────────────────────────────────────────────────┘
```

Module boundaries = Python packages (§17). No package may import from a package to its
right/above except through defined interfaces. The TUI imports only `core.events`,
`core.commands`, and read-only store queries.

---

## 4. Tech stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12, `uv` for env/deps | ecosystem for finance + LLM |
| Orchestration | LangGraph (no LangChain chains) | typed state, checkpointing, resume |
| LLM providers | `anthropic` + `openai` SDKs behind a gateway | two tiers: deep/quick |
| Data models | pydantic v2 | validation everywhere |
| TUI | **Textual** ≥ 0.60 + `textual-plotext` for charts | full app-grade TUI, async, testable |
| Market data | `yfinance` (primary), `stooq` via pandas-datareader (fallback), Alpha Vantage + Finnhub (key-gated, optional), local CSV/Parquet | free-first, fallback chain |
| News/sentiment | Finnhub news API (free tier) + yfinance news; Reddit via public JSON (best-effort) | free-first |
| Storage | SQLite (WAL mode) via `sqlite3` or `sqlmodel`; JSONL event logs | zero-ops |
| Backtest math | pandas + numpy | sufficient at daily bars |
| Config | `pydantic-settings`: `config.toml` + `.env` | typed config |
| Scheduling | internal asyncio scheduler (cron-like string) | no OS dependency |
| Testing | pytest + pytest-asyncio + `pytest-socket` (block net in tests) + Textual's `Pilot` | matches Vibe-Trading's discipline |
| Packaging | single `sentinel` console entry point | `uv run sentinel` |

---

## 5. Data layer

Package: `sentinel/data/`.

### 5.1 Interfaces

```python
class MarketDataLoader(Protocol):
    name: str
    def get_ohlcv(self, symbol: str, start: date, end: date, interval: str = "1d") -> pd.DataFrame: ...
    def get_quote(self, symbol: str) -> Quote: ...          # latest price snapshot
class NewsLoader(Protocol):
    def get_news(self, symbol: str, lookback_days: int, limit: int) -> list[NewsItem]: ...
class FundamentalsLoader(Protocol):
    def get_fundamentals(self, symbol: str) -> FundamentalsSnapshot: ...
```

### 5.2 Fallback chains (registry)

`DataRouter` resolves a request against an ordered chain; on exception or empty result it
falls through and records which provider served the request (surfaced in the TUI status bar).

- OHLCV equities: `yfinance → stooq → alphavantage(if key) → local`
- OHLCV crypto: `yfinance → local` (v1; ccxt is a v2 seam)
- News: `finnhub(if key) → yfinance_news → none`
- Fundamentals: `yfinance → alphavantage(if key) → none`

Missing optional providers degrade gracefully: the corresponding analyst report notes
"data unavailable" rather than the run failing.

### 5.3 Sanity guard (from Vibe-Trading)

At the router boundary, drop OHLCV bars where: `high < low`, any price ≤ 0,
`not (low <= open <= high)`, `not (low <= close <= high)`. Log a warning with count dropped.

### 5.4 Caching & snapshots

- Disk cache under `~/.sentinel/cache/` keyed by `(provider, symbol, interval, start, end)`;
  TTL: 15 min for intraday quotes, 24 h for daily bars ending before today, infinite for
  fully-historical windows.
- Every decision run pins a **DataSnapshot**: the exact frames/news fed to agents, saved as
  parquet+json under the run directory. This gives reproducibility (G5) and lets the TUI
  re-render exactly what agents saw.

### 5.5 Derived indicators

Computed locally (never trusted from an LLM): SMA(20/50/200), EMA(12/26), MACD(12,26,9),
RSI(14), Bollinger(20,2), ATR(14), OBV, 30-day realized volatility, 1/5/20-day returns.
Function: `compute_indicators(df) -> pd.DataFrame` (pure, unit-tested against known fixtures).

### 5.6 Core data models

```python
class Quote(BaseModel):        symbol: str; price: Decimal; ts: datetime; source: str
class NewsItem(BaseModel):     title: str; summary: str; source: str; url: str; published: datetime; symbols: list[str]
class FundamentalsSnapshot(BaseModel):
    symbol: str; as_of: date
    market_cap: float | None; pe_ttm: float | None; forward_pe: float | None
    eps_ttm: float | None; revenue_growth_yoy: float | None; profit_margin: float | None
    debt_to_equity: float | None; free_cash_flow: float | None
    next_earnings_date: date | None; analyst_target_mean: float | None
class DataSnapshot(BaseModel):
    run_id: str; symbol: str; as_of: datetime
    ohlcv_path: str; indicators_path: str
    news: list[NewsItem]; fundamentals: FundamentalsSnapshot | None
    providers_used: dict[str, str]
```

---

## 6. Agent layer

Package: `sentinel/agents/`. Every agent is a class with a single `run(state) -> Report`
method that: builds a prompt from typed inputs, calls the LLM gateway, parses the response
into a typed report (retry once on parse failure with the error appended), and emits
`AgentStarted/AgentCompleted` events.

### 6.1 Model tiers

- `quick` (default `claude-haiku-4-5-20251001` or `gpt-5.4-mini`): all four analysts,
  debate participants.
- `deep` (default `claude-sonnet-5` or `gpt-5.5`): Research Manager, Trader, Portfolio
  Manager.
- Both configurable per-role in `config.toml`. Temperature default 0.

### 6.2 Report envelope (all agents)

```python
class AgentReport(BaseModel):
    run_id: str; agent: str; model: str
    created_at: datetime; latency_ms: int
    input_tokens: int; output_tokens: int; cost_usd: Decimal
    content: str            # human-readable markdown body (shown in TUI)
    # + role-specific structured fields in subclasses below
```

### 6.3 Analyst team (parallel, quick tier)

All four run **concurrently** (asyncio.gather). Each receives only its slice of the
DataSnapshot, never raw everything.

**MarketAnalyst** — input: last 90 daily bars + indicator table (rendered as compact
markdown). Output fields: `trend: Literal["strong_up","up","sideways","down","strong_down"]`,
`support: float | None`, `resistance: float | None`, `signals: list[str]` (e.g. "MACD
bullish crossover on 2026-07-01"), `confidence: int` (0–100).
Prompt core: *"You are a technical analyst. Using ONLY the provided price/indicator data,
describe the current technical picture… Do not invent indicator values; every claim must
cite a value from the table."*

**FundamentalsAnalyst** — input: FundamentalsSnapshot. Output: `valuation:
Literal["cheap","fair","expensive","unknown"]`, `quality_flags: list[str]`,
`red_flags: list[str]`, `upcoming_catalysts: list[str]`, `confidence: int`.

**NewsAnalyst** — input: up to 20 NewsItems (title+summary only). Output:
`sentiment: Literal["very_negative","negative","neutral","positive","very_positive"]`,
`key_events: list[str]`, `macro_context: str`, `confidence: int`. Prompt must instruct:
weigh recency, ignore clickbait, flag if coverage is thin.

**SentimentAnalyst** — input: social/news chatter (best-effort; may be empty). Output:
`retail_mood: Literal[...same 5-scale...]`, `notable_narratives: list[str]`,
`confidence: int`. If input empty → returns neutral with confidence 0 (no LLM call).

### 6.4 Research debate (bull vs bear, quick tier)

State: shared `debate_transcript: list[DebateTurn]`.

```python
class DebateTurn(BaseModel): speaker: Literal["bull","bear"]; round: int; argument: str
```

- **BullResearcher** and **BearResearcher** alternate, bull first, for
  `max_debate_rounds` rounds (default 2, so 4 turns).
- Each sees: all four analyst reports + full transcript so far + **retrieved memory lessons**
  (§12.3) relevant to this symbol/setup.
- Prompt core (bull): *"Argue the strongest evidence-based case for taking/holding a long
  position. You MUST directly rebut the bear's latest points before adding new arguments.
  Cite specific data from the reports. Concede points you cannot rebut."* (Bear mirrored.)
- **ResearchManager** (deep tier) then judges: input = analyst reports + full transcript.
  Output:

```python
class InvestmentPlan(AgentReport):
    stance: Literal["bullish","bearish","neutral"]
    conviction: int                      # 0-100
    thesis: str                          # 3-5 sentences
    key_risks: list[str]
    invalidation: str                    # what observation would void the thesis
    debate_scorecard: str                # who argued better and why
```

### 6.5 Trader (deep tier)

Input: InvestmentPlan + current position in the symbol + portfolio summary (cash, exposure,
open positions) + memory lessons. Output:

```python
class TradeProposal(AgentReport):
    action: Literal["BUY","SELL","HOLD"]
    quantity_pct: float        # % of allowed max position size to deploy (0-100)
    order_type: Literal["market"]          # v1: market only
    time_horizon_days: int
    entry_rationale: str
    exit_plan: str                          # target + stop expressed in words
    stop_loss_pct: float | None             # e.g. 8.0 = exit if -8% from entry
    take_profit_pct: float | None
```

`HOLD` short-circuits: run completes with decision HOLD, no risk debate (configurable:
`always_run_risk_debate: false`).

### 6.6 Risk debate (quick tier) + Portfolio Manager gate (deep tier)

Three perspectives comment in sequence for `max_risk_discuss_rounds` rounds:
**AggressiveRisk** (argues for upside/urgency), **ConservativeRisk** (argues capital
preservation, worst cases), **NeutralRisk** (arbitrates, quantifies). Each sees the
TradeProposal, InvestmentPlan, portfolio state, and the risk transcript so far.

**PortfolioManager** (deep) issues the final verdict:

```python
class PMDecision(AgentReport):
    verdict: Literal["APPROVE","REVISE","REJECT"]
    approved_quantity_pct: float     # may cut the trader's size
    reasoning: str
    lessons_applied: list[str]       # which memory lessons influenced this
```

- `REVISE` = approve with reduced `approved_quantity_pct`.
- The PM's output is advisory input to the **Risk Engine** (§9) — the deterministic mandate
  check runs *after* PM approval and can still veto. LLMs never get the last word on risk.

### 6.7 Prompt engineering rules (apply to every agent)

1. System prompt states role, forbids fabricating data, requires citing provided values.
2. All numeric data delivered as compact markdown tables, dates ISO-formatted.
3. Response format: the structured fields as a JSON block matching the pydantic schema
   (use the provider's native structured-output/tool-use mode), plus the markdown `content`.
4. One retry on schema-parse failure, feeding back the validation error. Second failure →
   run enters `FAILED` state with the raw response persisted for debugging.
5. Full prompt templates live in `sentinel/agents/prompts/*.md` as files (not inline
   strings) so they can be edited without code changes; loaded with placeholder substitution.

---

## 7. Orchestration

Package: `sentinel/orchestrator/`.

### 7.1 Run state

```python
class RunState(BaseModel):
    run_id: str                      # ULID
    symbol: str; as_of: datetime; mode: Literal["decision","backtest_step"]
    status: Literal["pending","fetching_data","analyzing","debating",
                    "trading","risk_review","gating","executing",
                    "completed","failed","cancelled","halted"]
    snapshot: DataSnapshot | None
    analyst_reports: dict[str, AgentReport]
    debate_transcript: list[DebateTurn]
    investment_plan: InvestmentPlan | None
    trade_proposal: TradeProposal | None
    risk_transcript: list[DebateTurn]
    pm_decision: PMDecision | None
    final_order: Order | None            # None if HOLD/REJECT/veto
    error: str | None
    total_cost_usd: Decimal; total_tokens: int
```

### 7.2 Graph

Nodes: `fetch_data → analysts(parallel fan-out/fan-in) → research_debate(loop) →
research_manager → trader → [HOLD? → finalize] → risk_debate(loop) → portfolio_manager →
mandate_gate → execute → reflect_enqueue → finalize`.

- **Checkpointing**: LangGraph SQLite checkpointer, one DB per run under the run directory.
  `sentinel resume <run_id>` continues from the last completed node (G4).
- **Cancellation**: cooperative — orchestrator checks a cancel flag between nodes; TUI's
  cancel command sets it.
- **Kill switch**: checked before `fetch_data` and before `execute`; if engaged the run
  status becomes `halted`.

### 7.3 Concurrency model

One asyncio event loop shared by TUI and orchestrator (Textual is async-native). LLM and
data calls use async clients or `asyncio.to_thread`. Max 1 concurrent decision run in v1
(a queue holds additional requests); backtests run as a background task with progress
events.

---

## 8. Debate mechanics (normative details)

- Transcript is append-only; each turn capped at 350 words (instructed + hard truncation).
- Convergence early-exit: after each full round, a cheap classifier call (quick tier) asks
  "did the last round introduce materially new arguments? yes/no" — if no, debate ends early.
- The judge (ResearchManager) must fill `debate_scorecard` naming which specific arguments
  it found decisive; this is displayed in the TUI Debate view and stored for reflection.

---

## 9. Risk & safety layer

Package: `sentinel/risk/`. **All checks are pure deterministic functions.**

### 9.1 Mandate

Stored in `mandate.toml` at the project root; loaded at startup; editable only outside the
app (deliberate friction), TUI shows it read-only.

```toml
[mandate]
symbol_universe = ["AAPL","MSFT","NVDA","GOOGL","AMZN","SPY","QQQ","BTC-USD","ETH-USD"]
max_position_pct_equity = 10.0      # max % of total equity in one symbol
max_order_notional_usd  = 2000.0
max_gross_exposure_pct  = 80.0      # sum(|positions|)/equity
max_daily_loss_pct      = 3.0       # realized+unrealized day P&L floor → trading disabled for the day
max_orders_per_day      = 10
allow_short             = false
cooldown_minutes_per_symbol = 60    # min gap between orders in same symbol
```

### 9.2 Mandate gate

`check_order(order, portfolio, mandate) -> GateResult` where
`GateResult = passed: bool, violations: list[Violation]`. Every violation carries a machine
code (`SYMBOL_NOT_IN_UNIVERSE`, `ORDER_TOO_LARGE`, `EXPOSURE_CAP`, `DAILY_LOSS_HALT`,
`COOLDOWN`, `SHORT_NOT_ALLOWED`, `KILL_SWITCH`) + human message. A failed gate → order
rejected, run completes as `completed` with `final_order=None` and the violations recorded
and displayed prominently in the TUI.

### 9.3 Kill switch

File-based: if `~/.sentinel/KILL` exists, no run starts and no order executes.
TUI binding `K` toggles it (with confirmation modal); also `sentinel kill on|off`.
Engaging it also cancels the active run at the next node boundary.

### 9.4 Position sizing

Deterministic: `target_notional = equity * max_position_pct_equity/100 *
quantity_pct/100 * pm_scale` where `pm_scale = approved_quantity_pct/trader_quantity_pct`
(≤1). Quantity = floor to whole shares (crypto: 6 dp). Sizing math is code; agents only
choose percentages.

### 9.5 Stop management

A background `PositionMonitor` task (interval: 5 min during configured market hours,
15 min for crypto) evaluates each open position against its stored `stop_loss_pct` /
`take_profit_pct` / `time_horizon_days`; a trigger enqueues a **mechanical exit order**
(no LLM involved) which still passes the mandate gate (loss-halt exempted for
risk-reducing orders — closing trades are always allowed).

### 9.6 Audit ledger

Append-only JSONL `~/.sentinel/audit.jsonl`: every gate evaluation, order, fill, kill-switch
change, mandate load (with hash) — `{ts, kind, payload, actor}`. The TUI Logs screen tails it.

---

## 10. Execution layer

Package: `sentinel/execution/`.

### 10.1 Broker interface

```python
class Broker(Protocol):
    async def submit(self, order: Order) -> Fill | OrderRejected
    async def get_quote(self, symbol: str) -> Quote
```

### 10.2 PaperBroker (the only v1 implementation)

- Fills market orders at the latest quote with configurable slippage
  (`slippage_bps = 5`) and commission (`commission_usd = 0.0`).
- Refuses fills when no fresh quote is available (stale > 30 min during market hours).
- Portfolio accounting: positions (qty, avg cost), cash, realized/unrealized P&L, equity
  curve snapshot appended after every fill and at each monitor tick.

```python
class Order(BaseModel):
    order_id: str; run_id: str | None; symbol: str
    side: Literal["buy","sell"]; qty: Decimal; type: Literal["market"]
    reason: Literal["agent_decision","stop_loss","take_profit","time_exit","manual"]
    created_at: datetime
class Fill(BaseModel):
    order_id: str; price: Decimal; qty: Decimal; ts: datetime
    slippage_usd: Decimal; commission_usd: Decimal
class Position(BaseModel):
    symbol: str; qty: Decimal; avg_cost: Decimal
    stop_loss_pct: float | None; take_profit_pct: float | None
    opened_at: datetime; horizon_days: int | None; source_run_id: str | None
```

### 10.3 Live-broker seam *(informative)*

`AlpacaBroker` implementing the same Protocol is the designated v2 extension. The spec of
v1 must not assume PaperBroker internals anywhere outside `execution/`.

---

## 11. Backtesting engine

Package: `sentinel/backtest/`.

### 11.1 Two modes

1. **Agent replay** (`mode="agent"`): walk forward over trading days in `[start, end]` with
   `cadence` (default weekly); each step builds a DataSnapshot **strictly from data ≤ that
   date** (news filtered by published date; fundamentals as-of best effort) and runs the
   full pipeline against a dedicated backtest portfolio. Costs real LLM tokens — the TUI
   shows a cost estimate + confirmation before starting.
2. **Rule replay** (`mode="rule"`): free/fast; runs a deterministic strategy
   (`Strategy` protocol: `on_bar(context) -> Signal`) — ships with `sma_cross`,
   `rsi_meanrevert`, `buy_hold` as baselines for comparison.

### 11.2 Anti-lookahead

- Decisions computed on bar t execute at **open of bar t+1**.
- A sentinel test feeds a synthetic frame with a known future spike and asserts the engine
  cannot see it (adopted from Vibe-Trading).

### 11.3 Metrics & robustness

`BacktestResult`: total & annualized return, Sharpe (rf=0), Sortino, max drawdown (+dates),
win rate, profit factor, avg win/loss, exposure %, turnover, per-trade table, equity curve
series, benchmark (SPY or BTC-USD) comparison, alpha vs benchmark.
Optional bootstrap: resample daily returns (1000×) → 5th/95th percentile Sharpe & maxDD.

---

## 12. Memory & reflection

Package: `sentinel/memory/`.

### 12.1 Decision journal

Every completed decision run writes a `JournalEntry` row: symbol, date, stance, action,
conviction, size, thesis summary, invalidation, and — filled in later — realized outcome.

### 12.2 Outcome scoring

A daily task (and on-demand `sentinel reflect`) finds journal entries whose horizon has
elapsed (or position closed), computes realized return vs SPY over the same window, and
calls a deep-tier **ReflectionAgent**: input = the full run summary + outcome. Output:

```python
class Lesson(BaseModel):
    lesson_id: str; created_at: datetime
    symbol: str; setup_tags: list[str]        # e.g. ["earnings_runup","high_rsi","bull_won_debate"]
    what_happened: str; lesson: str            # one imperative sentence
    grade: Literal["good_call","bad_call","lucky","unlucky"]
```

### 12.3 Recall

Lessons stored in SQLite FTS5. Before debate and before the PM gate, retrieve top-5 lessons
by FTS match on symbol + setup tags, injected into prompts under a
"Lessons from your past trades" header. PM must list `lessons_applied`.

### 12.4 Hygiene

Max 500 lessons; beyond that, lowest-relevance (oldest, never-recalled) are archived.
`sentinel memory list|show|forget` subcommands.

---

## 13. Terminal GUI (TUI) — full specification

Package: `sentinel/tui/`. Framework: **Textual**. Launch: `sentinel` (no args) or
`sentinel tui`. This is the primary interface; the CLI subcommands (§16) are secondary.

### 13.1 Global layout

- **Header** (1 row): app name • mode badge `[PAPER]` • market clock (ET) + open/closed dot
  • kill-switch state (`● ARMED` red when engaged) • total equity + day P&L (green/red).
- **Tab bar**: `F1 Dashboard  F2 Run  F3 Portfolio  F4 History  F5 Backtest  F6 Memory  F7 Logs`.
- **Footer**: context-sensitive keybindings (Textual `Footer`).
- Dark theme default; respect terminal palette; all P&L coloring must also carry a +/- sign
  (never color-only).

### 13.2 Screen: Dashboard (F1)

```
┌ Sentinel ────────────────────────── PAPER │ NYSE OPEN 14:32 ET │ ⛔ off │ $10,412 +1.2% ┐
│ ┌─ Portfolio ────────────────┐ ┌─ Equity curve (30d) ────────────────────────────────┐ │
│ │ Cash        $4,210         │ │  10.6k ┤                                    ╭──╯    │ │
│ │ Equity      $10,412        │ │  10.2k ┤                      ╭────╮  ╭─────╯       │ │
│ │ Day P&L     +$122 (+1.2%)  │ │   9.8k ┤ ╭────╮   ╭──────╮╭───╯    ╰──╯             │ │
│ │ Open pos    3              │ │        └─┴──────┴────────┴──────────┴───────────────┘ │
│ │ Exposure    41% / 80%      │ └──────────────────────────────────────────────────────┘ │
│ └────────────────────────────┘ ┌─ Positions ─────────────────────────────────────────┐ │
│ ┌─ Quick actions ────────────┐ │ SYM    QTY   AVG     LAST    P&L      STOP   RUN    │ │
│ │ [R] Run decision…          │ │ NVDA   4     168.20  175.90  +$30.8   -8%    01J…   │ │
│ │ [B] Backtest…              │ │ AAPL   8     212.10  214.05  +$15.6   -8%    01J…   │ │
│ │ [K] Kill switch            │ │ BTC-USD 0.01 64,120  66,900  +$27.8   -10%   01J…   │ │
│ └────────────────────────────┘ └─────────────────────────────────────────────────────┘ │
│ ┌─ Recent decisions ──────────────────────────┐ ┌─ Today ────────────────────────────┐ │
│ │ 07-03 NVDA  BUY  APPROVE  4sh   $0.31 2m41s │ │ Orders 1/10 │ LLM spend $0.31      │ │
│ │ 07-01 TSLA  HOLD —        —     $0.22 1m58s │ │ Loss cap −3% (now +1.2%) │ Sched ✓ │ │
│ └─────────────────────────────────────────────┘ └────────────────────────────────────┘ │
└─ [R]un [B]acktest [K]ill [Q]uit ── F1..F7 screens ─────────────────────────────────────┘
```

Widgets: equity sparkline via `textual-plotext`; positions table = `DataTable`, row Enter →
symbol detail modal (price chart 90d + indicators + open orders + link to source run).
`[R]` opens the **New Run modal**: symbol input w/ universe autocomplete, date (default
today), depth preset (`fast` = 1 debate round quick-only / `standard` / `deep` = 3 rounds),
live cost estimate; Enter starts run and jumps to F2.

### 13.3 Screen: Run Monitor (F2) — the flagship view

Left rail: pipeline stage list with live status glyphs (✓ done, ▶ running + spinner,
○ pending, ✗ failed) and per-stage elapsed/cost. Right: content pane showing the selected
stage's report rendered as markdown.

```
┌ Run 01JZX… NVDA 2026-07-06 ── RUNNING analyzing (2/4 analysts done) ── $0.09 ── 0:47 ┐
│ ┌ Pipeline ─────────────┐ ┌ Report: Market Analyst ────────────────────────────────┐ │
│ │ ✓ Fetch data     2s   │ │ ## Technical picture                                    │ │
│ │ ▶ Analysts       45s  │ │ Trend: **up** (conf 72). Price 175.9 above SMA50…       │ │
│ │   ✓ market            │ │ Signals:                                                │ │
│ │   ✓ news              │ │ • MACD bullish crossover 2026-07-01                     │ │
│ │   ▶ fundamentals      │ │ • RSI 61 — room before overbought                       │ │
│ │   ○ sentiment         │ │ Support 168.4 / Resistance 181.0                        │ │
│ │ ○ Debate  (0/2 rnds)  │ │                                                         │ │
│ │ ○ Research manager    │ │ (↑↓ select stage, Enter expand, d=debate view)          │ │
│ │ ○ Trader              │ └─────────────────────────────────────────────────────────┘ │
│ │ ○ Risk debate         │ ┌ Live feed ──────────────────────────────────────────────┐ │
│ │ ○ PM gate             │ │ 14:32:41 analyst.fundamentals started (haiku-4.5)       │ │
│ │ ○ Mandate check       │ │ 14:32:39 analyst.news done 2,113 tok $0.021             │ │
│ │ ○ Execute             │ │ 14:32:12 snapshot pinned: yfinance (ohlcv), finnhub …   │ │
│ └───────────────────────┘ └─────────────────────────────────────────────────────────┘ │
└─ [c]ancel run  [d]ebate view  [o]pen run dir  [←→] stages ────────────────────────────┘
```

**Debate view** (`d`): chat-style transcript — bull turns left-aligned green accent, bear
right-aligned red accent, judge verdict card at the end with the scorecard; risk debate
below in the same style. Auto-follows while running.
Terminal states render a **Decision card**: action, size, PM verdict, mandate gate result
(each check ✓/✗ with its code), fill details or rejection reason.

### 13.4 Screen: Portfolio (F3)

Positions table (sortable), closed-trades table (entry/exit, P&L, holding days, source run),
allocation bar (per-symbol % of equity vs mandate cap), equity curve full-width with
drawdown subplot, stats row (total return, Sharpe-to-date, max DD, win rate). `x` on a
position = manual close (confirmation modal; order reason `manual`).

### 13.5 Screen: History (F4)

Master-detail: filterable run list (symbol/action/verdict/date) → detail replicates the Run
Monitor for the archived run, plus outcome annotation once reflected ("+4.2% over 10d vs
SPY +1.1% — graded good_call"). `e` exports run bundle to markdown.

### 13.6 Screens: Backtest (F5), Memory (F6), Logs (F7)

- **Backtest**: config form (mode, symbol(s), dates, cadence, depth or rule); agent-mode
  shows estimated LLM cost and requires typed confirmation `run`. Progress bar with ETA;
  results panel = metrics table + equity-vs-benchmark plot + trades table; past results
  list with compare (two side-by-side metric columns).
- **Memory**: lessons table (grade-colored), FTS search box, detail pane, `x` = forget.
- **Logs**: tail of audit ledger + app log, level filter, `/` search, pause-on-scroll.

### 13.7 TUI ↔ core integration contract

- Single process, single event loop. Core emits `Event` objects on an in-process
  `EventBus` (`asyncio.Queue` fanout): `RunStarted, StageChanged, AgentStarted,
  AgentCompleted, DebateTurnAdded, DecisionMade, GateEvaluated, OrderSubmitted, OrderFilled,
  QuoteTick, EquityUpdated, KillSwitchChanged, CostIncurred, LogLine, BacktestProgress`.
- The TUI must never poll SQLite for live state; it renders from events, and hydrates from
  the store only on screen mount / app start.
- Commands flow the other way through `CommandService`: `start_run, cancel_run,
  toggle_kill, close_position, start_backtest, reflect_now` — the same service the CLI
  subcommands call, so TUI and CLI cannot diverge.
- Testing: Textual `Pilot` tests for each screen (mount, key handling, event-driven update).

### 13.8 Keybindings (global)

`F1–F7` screens • `R` new run • `B` new backtest • `K` kill switch • `?` help overlay •
`ctrl+p` command palette (Textual built-in) • `Q`/`ctrl+c` quit (confirm if run active).

---

## 14. Configuration

`config.toml` (project root, checked in with safe defaults) + `.env` (secrets, gitignored)
loaded via pydantic-settings. Precedence: env > toml > defaults.

```toml
[llm]
provider = "anthropic"              # anthropic | openai | openai_compatible
deep_model = "claude-sonnet-5"
quick_model = "claude-haiku-4-5-20251001"
temperature = 0.0
max_retries = 3
monthly_budget_usd = 25.0           # hard stop: runs refuse to start past budget

[pipeline]
max_debate_rounds = 2
max_risk_discuss_rounds = 1
always_run_risk_debate = false
depth_presets = { fast = 1, standard = 2, deep = 3 }

[data]
alpha_vantage_key_env = "ALPHA_VANTAGE_KEY"   # optional
finnhub_key_env = "FINNHUB_KEY"               # optional
news_limit = 20
news_lookback_days = 7

[execution]
slippage_bps = 5
commission_usd = 0.0
starting_cash_usd = 10000.0

[schedule]
enabled = false
decision_cron = "30 9 * * 1-5"      # 09:30 ET weekdays
watchlist = ["NVDA","AAPL","SPY"]

[monitor]
equity_interval_min = 5
crypto_interval_min = 15
```

`.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPHA_VANTAGE_KEY`, `FINNHUB_KEY`.
`sentinel doctor` prints a redacted readiness report (keys present, providers reachable,
DB writable, mandate valid) — copied from Vibe-Trading's `provider doctor`.

---

## 15. Persistence

Root: `~/.sentinel/` — `sentinel.db` (SQLite, WAL), `runs/<run_id>/` (snapshot parquet,
checkpoint db, `events.jsonl`, `llm_usage.json`, raw agent responses), `audit.jsonl`,
`cache/`, `KILL` (flag file).

SQLite tables (DDL the builder must implement, abridged):

```sql
CREATE TABLE runs      (run_id TEXT PRIMARY KEY, symbol TEXT, as_of TEXT, mode TEXT,
                        status TEXT, action TEXT, verdict TEXT, cost_usd REAL,
                        tokens INTEGER, created_at TEXT, finished_at TEXT);
CREATE TABLE reports   (run_id TEXT, agent TEXT, model TEXT, content TEXT,
                        structured_json TEXT, tokens_in INT, tokens_out INT,
                        cost_usd REAL, latency_ms INT, created_at TEXT);
CREATE TABLE orders    (order_id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, side TEXT,
                        qty TEXT, reason TEXT, status TEXT, created_at TEXT);
CREATE TABLE fills     (order_id TEXT, price TEXT, qty TEXT, slippage_usd TEXT,
                        commission_usd TEXT, ts TEXT);
CREATE TABLE positions (symbol TEXT PRIMARY KEY, qty TEXT, avg_cost TEXT, stop_pct REAL,
                        tp_pct REAL, horizon_days INT, opened_at TEXT, source_run_id TEXT);
CREATE TABLE equity_curve (ts TEXT, equity REAL, cash REAL, day_pnl REAL);
CREATE TABLE journal   (run_id TEXT PRIMARY KEY, horizon_end TEXT, realized_ret REAL,
                        bench_ret REAL, graded TEXT, reflected_at TEXT);
CREATE VIRTUAL TABLE lessons USING fts5(lesson_id, symbol, setup_tags, what_happened,
                        lesson, grade, created_at);
CREATE TABLE backtests (bt_id TEXT PRIMARY KEY, config_json TEXT, metrics_json TEXT,
                        equity_json TEXT, created_at TEXT);
CREATE TABLE costs     (ts TEXT, run_id TEXT, agent TEXT, model TEXT, tokens_in INT,
                        tokens_out INT, cost_usd REAL);
```

Money as TEXT-decimal or integer cents — never float — in orders/fills/positions.

---

## 16. CLI subcommands (secondary interface)

`sentinel` → TUI. Plus headless commands (all route through `CommandService`):
`run <SYMBOL> [--date] [--depth]`, `resume <run_id>`, `backtest --mode ... --symbol ...`,
`portfolio`, `history [--symbol]`, `reflect`, `memory list|show|search|forget`,
`kill on|off`, `doctor`, `export <run_id>`. Headless output: rich-formatted tables;
`--json` flag for machine output.

---

## 17. Directory structure

```
sentinel/
├── pyproject.toml               # uv-managed; console_script sentinel=sentinel.__main__:main
├── config.toml
├── mandate.toml
├── .env.example
├── docs/DECISIONS.md
├── sentinel/
│   ├── __main__.py              # CLI dispatch (typer) → TUI default
│   ├── core/                    # events.py, commands.py, models.py, bus.py, ids.py
│   ├── config/                  # settings.py (pydantic-settings)
│   ├── data/                    # router.py, loaders/{yfinance,stooq,alphavantage,finnhub,local}.py,
│   │                            # indicators.py, sanity.py, cache.py, snapshot.py
│   ├── llm/                     # gateway.py, providers/{anthropic,openai}.py, cost.py, budget.py
│   ├── agents/                  # base.py, analysts.py, researchers.py, trader.py,
│   │                            # risk_debaters.py, portfolio_manager.py, reflection.py
│   │   └── prompts/*.md
│   ├── orchestrator/            # graph.py, state.py, runner.py, scheduler.py
│   ├── risk/                    # mandate.py, gate.py, killswitch.py, sizing.py, monitor.py
│   ├── execution/               # broker.py (Protocol), paper.py, portfolio.py, ledger.py
│   ├── backtest/                # engine.py, strategies/, metrics.py, bootstrap.py
│   ├── memory/                  # journal.py, reflection_job.py, recall.py
│   ├── store/                   # db.py, migrations.py, repos/*.py
│   └── tui/                     # app.py, screens/{dashboard,run,portfolio,history,
│                                #   backtest,memory,logs}.py, widgets/, styles.tcss
└── tests/                       # mirrors package layout + fixtures/ (recorded data)
```

---

## 18. Testing strategy

- **No network in tests** (`pytest-socket`); all provider calls mocked/recorded fixtures.
- Unit: indicators vs known-good fixture values; mandate gate — table-driven case per
  violation code; sizing; OHLC sanity guard; cost metering math.
- Property: portfolio accounting invariants (cash+positions=equity; no negative cash;
  buy-then-sell round-trip P&L = price diff − costs).
- Anti-lookahead sentinel test (§11.2). Schema-parse retry path with a deliberately
  malformed LLM response.
- Integration: full decision run against a `FakeLLM` (canned per-agent responses) + fixture
  data → asserts final state, DB rows, events emitted in order.
- TUI: Textual Pilot per screen; snapshot tests for the Run Monitor terminal states.
- Target: ≥85% coverage on `risk/`, `execution/`, `backtest/`; CI = `uv run pytest` +
  `ruff` + `pyright` (strict on `risk/` and `execution/`).

---

## 19. Implementation phases & acceptance criteria

**Phase 0 — Skeleton (½ day equiv.)**: repo layout, pyproject, config loading, DB
migrations, event bus, `sentinel doctor`. ✔ `doctor` runs clean; empty TUI shell launches
with header/tabs/footer.

**Phase 1 — Data layer**: loaders, router+fallback, sanity guard, cache, indicators,
snapshot pinning. ✔ `snapshot NVDA` (dev command) produces parquet+json; unit tests pass;
pulling with yfinance blocked (simulated) still succeeds via stooq.

**Phase 2 — LLM gateway + one analyst**: gateway with retries, structured output, cost
metering, budget stop; MarketAnalyst end-to-end. ✔ real call produces a valid
`AgentReport`; cost row lands in `costs`; malformed-response retry test passes.

**Phase 3 — Full pipeline (headless)**: all agents, debates, LangGraph wiring,
checkpointing, mandate gate, PaperBroker, journal write. ✔ `sentinel run NVDA` completes
< 4 min, < $0.50 (standard depth); kill-switch mid-run → `halted`; `resume` finishes it;
FakeLLM integration test green.

**Phase 4 — TUI core**: Dashboard + Run Monitor + Portfolio live over the event bus.
✔ start a run from the TUI and watch every stage/debate turn stream in; decision card +
gate results render; positions/equity update after fill; Pilot tests green.

**Phase 5 — Backtesting**: rule engine + metrics + bootstrap; agent replay behind cost
confirmation; Backtest screen. ✔ `sma_cross` on SPY 2020-2024 reproduces deterministic
metrics across two runs; lookahead sentinel green; TUI shows progress + results + compare.

**Phase 6 — Memory & reflection**: journal scoring, ReflectionAgent, FTS recall into
prompts, Memory screen. ✔ a matured decision gets graded; its lesson visibly appears in
the next run's PM prompt (assert via stored prompt) and `lessons_applied`.

**Phase 7 — Scheduler, monitor & polish**: cron decisions, PositionMonitor stops,
History/Logs screens, exports, README with disclaimer. ✔ scheduled watchlist run fires;
a stop-loss triggers a mechanical exit in paper; full-suite CI green.

---

## 20. Appendix A — prompt template skeletons *(normative structure, tune wording freely)*

Every prompt file: `{role_definition}\n{hard_rules}\n{context_blocks}\n{task}\n{output_schema_instructions}`.
Hard rules (all agents): "Use only data provided below. Never invent numbers. Cite the
specific value behind every quantitative claim. If data is missing, say so and lower your
confidence." Context blocks are labeled `### <BLOCK NAME>` sections. Output = provider
structured-output mode against the pydantic schema.

## 21. Appendix B — known risks *(informative)*

- yfinance is unofficial and breaks periodically → fallback chain + `doctor` connectivity
  check + local CSV escape hatch.
- LLM cost creep in agent-mode backtests → budget hard-stop + pre-run estimates + typed
  confirmation.
- Textual API churn → pin version in lockfile.
- Overfitting to reflection lessons (self-reinforcing bias) → cap 5 lessons/prompt, include
  grade balance (mix good/bad calls) in recall.

*Research use only. Nothing in this system constitutes financial advice.*

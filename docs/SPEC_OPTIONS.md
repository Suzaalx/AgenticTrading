# Sentinel × Robinhood — Agentic Investing Spec v2.0 (options + staged live trading)

**Audience:** implementation agents (coder agents). Each workstream (WS) below is a
self-contained brief: context, contracts, files, acceptance criteria, dependencies. An
orchestrator should dispatch WS0 first, then WS1–WS2 in parallel, then the rest per the
dependency graph in §4.

**Prepared:** 2026-07-07. Verified against the current source tree (`master`, `ad2310e`) and
Robinhood's public developer surfaces as of July 2026.

**Supersedes:** the v1.0 paper-options spec. All v1.0 options content is retained (WS0–WS10);
v2.0 changes the execution posture from "paper only, forever" to a **staged live path on
Robinhood's official rails**, and adds WS11–WS14.

---

## 1. Mission & guardrails

Turn Sentinel into a **personal agentic investing system that can trade real money on
Robinhood** — deliberately, in stages — while adding **options** as a first-class asset class.
The agent pipeline proposes trades (stock or defined-risk option structures); deterministic
code prices, sizes, gates, executes, monitors, and settles; the reflection loop learns from
real outcomes; the TUI shows everything including live-account state.

### 1.1 The Robinhood reality (verified July 2026 — design to it, not around it)

| Rail | Status | Sentinel usage |
|---|---|---|
| **Robinhood Crypto Trading API** | Official, GA. API key + Ed25519 request signing (`x-api-key`, `x-signature`, `x-timestamp` — already scaffolded in `execution/robinhood.py`). Market/limit orders, best bid/ask, holdings. BTC/ETH in scope for Sentinel. | **Stage 1 live rail** (crypto) |
| **Robinhood Agentic Trading** (launched May 2026, beta) | Official. AI agents connect via Robinhood's **MCP servers** to a **dedicated agentic account** with user-controlled funding limits, trade preview, optional per-trade manual approval, push notifications, real-time activity feed, one-tap disconnect. **Equities only in beta**; options, crypto, event contracts, futures "coming soon." | **Stage 2 live rail** (equities); **Stage 3** (options, when RH ships it) |
| Unofficial APIs (`robin_stocks` etc., reverse-engineered) | Violates ToS; account-lockout risk; brittle auth (2FA/device approval). | **Rejected. Never used.** Record in `docs/DECISIONS.md`. |

Consequence: **options execution remains paper-only until Robinhood's agentic platform
supports options.** Everything else in the options workstreams (math, chains, candidates,
gating, monitoring, backtests) is built now and is rail-agnostic; when RH ships options on MCP,
only WS14's connector grows a new capability.

### 1.2 Non-negotiable design rules (violating any is a review-blocking defect)

1. **Determinism at the edges.** All pricing math (Black-Scholes-Merton, Greeks, IV inversion,
   binomial trees, payoff/max-loss math) is deterministic Python in `sentinel/options/` with
   golden-value tests. **The LLM never prices an option, never computes a Greek, never computes
   max loss, and never talks to a broker.** Agents choose among pre-computed candidates; only
   gated deterministic code reaches a live rail.
2. **Defined-risk only, forever.** No naked short calls/puts (cash-secured puts allowed —
   collateral escrowed). Max loss finite and computed before the gate runs. Mandate rule with a
   machine violation code, not a prompt instruction.
3. **Paper-first, staged live.** Paper remains the default and the permanent fallback. Live
   trading unlocks per asset class through the graduation ladder (§2) — each stage requires
   explicit config + mandate + CLI confirmation + a paper track record check. There is no
   single "go live" switch.
4. **The mandate gate cannot be bypassed** and runs after PM approval. In live mode a second,
   stricter live-mandate overlay applies (§WS12). The kill switch halts new orders **and
   cancels open live orders**.
5. **Official rails only.** Robinhood Crypto Trading API and Robinhood Agentic Trading MCP.
   No unofficial endpoints, no credential scraping, no web automation.
6. **Loud provenance.** Live fills, paper fills, and synthetic backtest prices are labeled on
   every artifact, report, and TUI surface (`execution_venue: "paper" | "robinhood_crypto" |
   "robinhood_agentic"`, `pricing_source: "live_chain" | "synthetic_bsm"`). Never silently mix.
7. **The broker is the source of truth in live mode.** Sentinel's SQLite is a mirror,
   reconciled every cycle (WS13). On divergence: halt new risk, alert, never "fix" silently.
8. **Everything is an event**; the TUI subscribes, never computes.
9. Respect remaining non-goals: single user, US equities/ETFs + BTC/ETH (options on equity/ETF
   underlyings only), daily cadence not HFT, research-use disclaimer ships everywhere. This is
   the user's own money in their own account — Sentinel is a personal tool, not a service.

### 1.3 v1 option strategy menu (exhaustive — anything else is out of scope)

| id | structure | legs | max loss (per spread, ×100 multiplier) |
|---|---|---|---|
| `long_call` | buy 1 call | 1 | premium paid |
| `long_put` | buy 1 put | 1 | premium paid |
| `covered_call` | own 100 shares + sell 1 call | 1 opt + stock | stock downside to 0 − premium (requires shares held) |
| `cash_secured_put` | sell 1 put + escrow `strike×100` cash | 1 | strike×100 − premium |
| `bull_call_spread` | buy call K1, sell call K2>K1 | 2 | net debit |
| `bear_put_spread` | buy put K2, sell put K1<K2 | 2 | net debit |
| `bull_put_spread` (credit) | sell put K2, buy put K1<K2 | 2 | (K2−K1)×100 − net credit |
| `bear_call_spread` (credit) | sell call K1, buy call K2>K1 | 2 | (K2−K1)×100 − net credit |
| `long_straddle` | buy call + put same K | 2 | total premium paid |
| `long_strangle` | buy OTM call + OTM put | 2 | total premium paid |

v1.1 (stub the enum values, do not implement): `iron_condor`, `calendar_spread`.

---

## 2. The graduation ladder (product spine of v2.0)

Each stage is independently gated per asset class. A stage unlocks only when **all** its
prerequisites hold, checked deterministically by `sentinel graduate <asset_class>` (WS12):

| Stage | Scope | Rail | Prerequisites (all enforced in code) |
|---|---|---|---|
| **0. Paper** (default) | everything | PaperBroker | none — permanent fallback |
| **1. Live crypto** | BTC-USD, ETH-USD spot | Crypto Trading API | ≥ 30 paper runs on crypto; paper crypto P&L history present; keys pass `sentinel doctor`; live-mandate section filled; typed CLI confirmation (`I UNDERSTAND LIVE CRYPTO`) |
| **2. Live equities** | mandate stock universe | Agentic Trading MCP | ≥ 60 days paper equity track; agentic account linked + funded (funding cap ≤ user's configured max); RH trade-preview/manual-approve setting chosen explicitly; typed confirmation |
| **3. Live options** | §1.3 menu | Agentic Trading MCP (**when RH ships options**) | Stage 2 active ≥ 30 days; ≥ 50 paper option positions closed; capability probe confirms RH options support; typed confirmation |

Modes can mix: e.g. live crypto + paper equities + paper options. `ExecutionRouter` (WS11)
routes per order. Every run banner, report, and export states the mode per asset class.

Robinhood's own agentic-account controls (funding limit, per-trade approval, activity feed,
one-tap disconnect) are treated as **defense in depth layered on top of Sentinel's gate — never
a replacement for it**. Sentinel's live mandate must be at least as strict as the RH funding cap.

---

## 3. Architecture deltas (bird's eye)

```
data/loaders/yfinance_options.py ─┐
data/loaders/rates.py (risk-free) ─┤→ DataRouter.get_option_chain() → OptionChainSnapshot
                                   │        (persisted per run, provenance-tagged)
sentinel/options/                  │
  pricing.py   (BSM, CRR binomial) │
  greeks.py                        ├→ enrich chain with locally computed Greeks
  iv.py        (Brent inversion,   │   (yfinance gives IV but NOT Greeks — we compute)
                IV rank, RV est.)  │
  strategies.py(payoff/max-loss)   │
  candidates.py(deterministic)     ─→ 3–6 candidate structures fed to agents

orchestrator: fetch_data → analysts(+OptionsAnalyst) → debate → research_manager →
  trader (TradeProposal | OptionStrategyProposal) → risk_debate → PM →
  mandate_gate (+ live overlay) → ExecutionRouter → reflect_enqueue → finalize

execution/
  router.py            ExecutionRouter: (asset_class, stage) → broker
  paper.py             Stage 0 (unchanged + options fills)
  robinhood_crypto.py  Stage 1: wire existing scaffold to live Crypto API
  robinhood_agentic.py Stage 2/3: MCP client → agentic account
  reconcile.py         live positions/orders/cash reconciliation loop

risk/monitor.py: option lifecycle ticks + live-order tracking (partial fills, working orders)
risk/live_mandate.py: stricter overlay applied only to live-routed orders
```

---

## 4. Workstream dependency graph

```
WS0  (prereq fixes)                       — first, blocks live work especially
WS1  (options math core)                  — no deps, pure math
WS2  (contracts & models)                 — no deps
WS3  (data layer: chains+rates)           — needs WS2
WS4  (risk: mandate+gate+sizing)          — needs WS1, WS2
WS5  (paper execution & lifecycle)        — needs WS1–WS4
WS6  (agents & prompts)                   — needs WS1–WS3, WS2
WS7  (orchestrator integration)           — needs WS4, WS5, WS6
WS8  (backtest: synthetic seam)           — needs WS1, WS5
WS9  (TUI + export)                       — needs WS7; live panels need WS11–WS13
WS10 (docs, README, disclaimer)           — last
WS11 (ExecutionRouter + broker protocol)  — needs WS2; gates WS12–WS14
WS12 (live safety: mandate overlay,
      graduation, secrets, kill-cancel)   — needs WS4, WS11
WS13 (reconciliation & live order
      lifecycle)                          — needs WS11, WS12
WS14 (Robinhood connectors: crypto API,
      agentic MCP)                        — needs WS11–WS13
```

Critical path to first live trade: WS0 → WS11 → WS12 → WS13 → WS14(crypto). Options
workstreams (WS1–WS8) run in parallel and land paper-first. Every WS lands with tests green
(`uv run pytest`), `ruff` clean, `pyright` strict-clean on `sentinel/options/`,
`sentinel/risk/`, `sentinel/execution/`.

---

## WS0 — Prerequisite fixes (now blocking: these were "polish" for paper, they are safety for live)

1. **Daily-loss halt verification.** `orchestrator/graph.py:390` passes `day_pnl_pct=None` into
   `check_order`. Verify with an end-to-end test that a portfolio down >
   `max_daily_loss_pct` actually produces `DAILY_LOSS_HALT` through the live gate path. Fix the
   plumbing if not. **A live system with an unverified daily-loss halt must not ship.**
2. **Loud synthetic provenance.** Add `data_quality: Literal["live","delayed","synthetic"]` to
   `DataSnapshot`, propagate to reports and TUI. **Refuse to execute** (analysis still allowed)
   when quality == "synthetic" in decision mode; in live mode this refusal is unconditional
   (no settings override).
3. **Closed-trade P&L `$0.00` TUI bug** — fix before WS9; live P&L rides on the same tables.
4. **Stale-quote tightening for live**: the 30-min staleness rule is fine for paper; live
   orders require quotes ≤ 60s old (enforced in WS12's overlay, but plumb the quote timestamp
   through now).

Acceptance: four targeted tests; existing 117 stay green.

---

## WS1 — Options math core (`sentinel/options/`) — pure, deterministic, no I/O

New package, no imports from data/execution layers (only `decimal`, `math`, `datetime`,
pydantic models from WS2). Float math internally is fine (industry norm); **money crossing
package boundaries is `Decimal`**.

### WS1.1 `pricing.py` — Black-Scholes-Merton + binomial

European BSM with continuous dividend yield `q`:

```
d1 = [ln(S/K) + (r − q + σ²/2)·T] / (σ·√T)
d2 = d1 − σ·√T
call = S·e^(−qT)·N(d1) − K·e^(−rT)·N(d2)
put  = K·e^(−rT)·N(−d2) − S·e^(−qT)·N(−d1)
```

`N` = standard normal CDF via `math.erf` (`N(x) = 0.5·(1 + erf(x/√2))`), `T` in year fractions
(ACT/365, valuation ts → expiry 16:00 ET), `r` continuously compounded, `σ` annualized.

Edge handling (explicit + tested): `T ≤ 0` → intrinsic; `σ ≤ 0` → discounted forward
intrinsic; clamp d1/d2 to ±40 against overflow.

**Put-call parity** helper + invariant test: `C − P = S·e^(−qT) − K·e^(−rT)`.

**American options** (US equity options are American): Cox-Ross-Rubinstein binomial,
`N = 200` steps default:

```
Δt = T/N;  u = e^(σ√Δt);  d = 1/u;  p = (e^((r−q)Δt) − d) / (u − d)
node value = max(exercise value, e^(−rΔt)·[p·V_up + (1−p)·V_down])
```

API: `bsm_price(S, K, T, r, q, sigma, kind)`, `crr_price(..., steps=200, american=True)`,
`intrinsic(S, K, kind)`, `parity_gap(...)`. Rule of use: **market chain data is the mark when
available; model price is for IV inversion, Greeks, backtest synthesis, and sanity bounds —
never overrides a live quote.**

### WS1.2 `greeks.py`

Closed-form BSM Greeks (dividend-adjusted), per-1.00 natural units + display conversions:

```
delta_call = e^(−qT)·N(d1)          delta_put = e^(−qT)·(N(d1) − 1)
gamma      = e^(−qT)·φ(d1) / (S·σ·√T)
vega       = S·e^(−qT)·φ(d1)·√T                     (per 1.00 vol; /100 = per vol point)
theta_call = −S·e^(−qT)·φ(d1)·σ/(2√T) − r·K·e^(−rT)·N(d2) + q·S·e^(−qT)·N(d1)   (per yr; /365 per day)
theta_put  = −S·e^(−qT)·φ(d1)·σ/(2√T) + r·K·e^(−rT)·N(−d2) − q·S·e^(−qT)·N(−d1)
rho_call   =  K·T·e^(−rT)·N(d2)      rho_put = −K·T·e^(−rT)·N(−d2)
```

Plus `position_greeks(legs, ...)`: signed per-leg Greeks × contracts × 100 → net
delta/gamma/vega/theta per position and portfolio (WS4 caps, WS9 display).

### WS1.3 `iv.py`

* `implied_vol(price, S, K, T, r, q, kind) -> float | None` — **Brent's method** on σ ∈
  [1e-4, 5.0] (Brent over Newton: vega → 0 deep ITM/OTM breaks Newton). `None` on
  no-arbitrage violations (`price < discounted intrinsic`, `price > S·e^(−qT)` calls /
  `> K·e^(−rT)` puts).
* Realized-vol estimators: close-to-close (`std ln-returns × √252`), **Parkinson**
  (`√(1/(4·ln2)·mean(ln(H/L)²))·√252`), **Yang-Zhang** (default; handles overnight gaps).
* `iv_rank(current, history)` = `(iv − min)/(max − min)` over 252 obs; `iv_percentile` =
  fraction below current. Persist per-run ATM IV to `iv_history(symbol, date, atm_iv, rv_yz)`.

### WS1.4 `strategies.py`

Per §1.3 strategy id: leg builder + deterministic `net_premium` (debit>0/credit<0),
`max_loss(legs, stock_qty=0)`, `max_gain`, `breakevens` — closed-form (bull call spread: max
loss = net debit, breakeven = K1 + debit/100; credit put spread: max loss = (K2−K1)×100 −
credit, breakeven = K2 − credit/100; straddle breakevens K ± total premium/100). Plus
`payoff_at_expiry(legs, S_T)` piecewise-linear as ground truth: property test that payoff-grid
max/min matches `max_gain`/`−max_loss` within 1e-6.

### WS1.5 `candidates.py` — deterministic candidate builder

Input: `OptionChainSnapshot`, direction (`bullish|bearish|neutral`), conviction, IV context,
settings. Output: 3–6 `OptionStrategyCandidate`s with all economics pre-computed. Rules
(deterministic, configurable):

* Expiry: nearest monthly with DTE in `[min_dte, max_dte]` (default 21–60).
* Strikes by target delta (long single-leg ≈ 0.60Δ; spread short leg ≈ 0.30Δ; strangle ≈
  0.25Δ), snapped to listed strikes.
* Hard liquidity filter: `open_interest ≥ min_oi` (default 100), `bid > 0`,
  `(ask−bid)/mid ≤ max_spread_pct` (default 10%). Failing candidates are never shown to agents.
* Regime mapping: IV rank > 0.5 + direction → credit spreads / CSP; low IV rank → debit
  structures; neutral + event within DTE → straddle/strangle.

### WS1 acceptance

Golden values (1e-3): `S=100, K=100, T=1, r=0.05, q=0, σ=0.20` → call `10.4506`, put `5.5735`,
call Δ `0.6368`, Γ `0.01876`, vega `0.3752`/pt, call θ `−0.01757`/day, call ρ `0.5323`/pt·100.
Parity to 1e-9 across a grid. `implied_vol(bsm_price(σ=0.37)) == 0.37 ± 1e-6`. CRR European →
BSM within 0.05% at 200 steps; American put ≥ European. Payoff-grid property tests. No
network, no I/O.

---

## WS2 — Contracts & persistence (`core/models.py`, migrations)

New pydantic models (same `SentinelModel` base, `Decimal` for money):

```python
OptionKind = Literal["call", "put"]

class OptionContract(SentinelModel):
    contract_symbol: str          # OCC: AAPL260918C00230000
    underlying: str
    kind: OptionKind
    strike: Decimal
    expiry: date
    multiplier: int = 100

class OptionQuote(SentinelModel):
    contract: OptionContract
    bid: Decimal; ask: Decimal; last: Decimal | None
    volume: int; open_interest: int
    implied_vol: float | None      # provider IV — sanity-checked, may be None
    ts: datetime; source: str
    model_iv: float | None; delta: float | None; gamma: float | None
    vega: float | None; theta: float | None      # locally computed (WS1)

class OptionChainSnapshot(SentinelModel):
    run_id: str; underlying: str; as_of: datetime
    spot: Decimal; risk_free_rate: float; dividend_yield: float
    expiries: list[date]
    quotes: list[OptionQuote]              # near-the-money window only (WS3)
    atm_iv: float | None; iv_rank: float | None; iv_percentile: float | None
    rv_yang_zhang: float | None
    pricing_source: Literal["live_chain", "synthetic_bsm"]
    providers_used: dict[str, str]

class OptionLeg(SentinelModel):
    contract: OptionContract
    side: Literal["buy", "sell"]
    contracts: int
    limit_price: Decimal | None             # None = mid

class OptionStrategyCandidate(SentinelModel):
    strategy: OptionStrategyId              # Literal of §1.3 ids + v1.1 stubs
    legs: list[OptionLeg]
    net_premium: Decimal                    # debit>0/credit<0, ×100 included
    max_loss: Decimal; max_gain: Decimal | None
    breakevens: list[Decimal]
    est_pop: float | None                   # documented 1−N(d2)-style proxy
    net_delta: float; net_vega: float; net_theta: float
    liquidity_score: float
    rationale_facts: str                    # deterministic one-liner for prompts

class OptionStrategyProposal(AgentReport):  # Trader output, parallel to TradeProposal
    action: Literal["OPEN", "CLOSE", "HOLD"]
    strategy: OptionStrategyId
    legs: list[OptionLeg]
    candidate_id: str                       # MUST reference an offered candidate
    max_loss_usd: Decimal                   # gate re-verifies from legs
    time_horizon_days: int
    entry_rationale: str; exit_plan: str
    stop_loss_pct_premium: float | None
    take_profit_pct_premium: float | None

class OptionPosition(SentinelModel):
    position_id: str; underlying: str; strategy: OptionStrategyId
    legs: list[OptionLeg]
    open_premium: Decimal                   # signed net, ×100×contracts
    max_loss: Decimal; collateral: Decimal
    opened_at: datetime; expiry: date; horizon_days: int | None
    stop_loss_pct_premium: float | None; take_profit_pct_premium: float | None
    source_run_id: str | None
    venue: Literal["paper", "robinhood_agentic"] = "paper"     # v2.0
```

`Order` extension (backward compatible): `asset_type: Literal["equity","option","crypto"] =
"equity"`, `legs: list[OptionLeg] | None = None`, `strategy: OptionStrategyId | None = None`,
`type` widened to `Literal["market","limit","net_debit","net_credit"]`,
`client_order_id: str` (idempotency key, WS13), `venue: Literal["paper","robinhood_crypto",
"robinhood_agentic"] = "paper"`. `Fill` gains `venue` + `broker_order_id: str | None`.
`RunState` gains `option_chain`, `option_candidates`, `option_proposal`.
`JournalEntry.action` widens to `"OPEN_OPTION"`/`"CLOSE_OPTION"`; add `strategy`, `venue`.

Mandate additions (`Mandate` + `mandate.toml`, safe defaults):

```toml
[mandate.options]
enabled = false
underlying_universe = ["AAPL","MSFT","NVDA","GOOGL","AMZN","SPY","QQQ"]
defined_risk_only = true             # validator rejects false — cannot be configured off
max_loss_per_position_usd = 500.0
max_total_options_max_loss_pct_equity = 15.0
max_contracts_per_order = 10
min_open_interest = 100
max_rel_spread_pct = 10.0
min_dte = 21
max_dte = 60
max_net_portfolio_delta_abs = 200.0
max_net_portfolio_vega_abs  = 500.0
max_option_orders_per_day = 4

[mandate.live]                        # v2.0 — overlay for live-routed orders only (WS12)
crypto_stage_enabled = false
equity_stage_enabled = false
options_stage_enabled = false
max_live_order_notional_usd = 200.0   # start tiny; user raises deliberately
max_live_daily_loss_usd = 100.0
max_live_orders_per_day = 3
max_account_allocation_usd = 2000.0   # ≤ RH agentic funding cap
require_limit_orders = true           # live market orders forbidden by default
quote_max_age_seconds = 60
```

New `ViolationCode` values: `OPTIONS_DISABLED`, `NAKED_SHORT_OPTION`, `MAX_LOSS_EXCEEDED`,
`OPTIONS_BUDGET_EXCEEDED`, `ILLIQUID_CONTRACT`, `DTE_OUT_OF_RANGE`, `GREEKS_CAP_EXCEEDED`,
`UNDERLYING_NOT_ALLOWED`, `INSUFFICIENT_COLLATERAL`, `EXPIRY_TOO_CLOSE`, and (v2.0)
`LIVE_STAGE_NOT_ENABLED`, `LIVE_NOTIONAL_EXCEEDED`, `LIVE_DAILY_LOSS_HALT`,
`LIVE_ORDER_LIMIT`, `QUOTE_TOO_OLD`, `RECONCILIATION_MISMATCH`, `VENUE_CAPABILITY_MISSING`.

Migrations (idempotent, money as TEXT-decimal): `option_positions`, `option_chain_meta`,
`iv_history`, and (v2.0) `live_orders(client_order_id PK, broker_order_id, venue, status,
submitted_at, last_sync_at, raw_json)`, `reconciliations(ts, venue, ok, diff_json)`. Per-run
artifact: `runs/<id>/chain.parquet`.

Acceptance: model round-trips; validator rejects `defined_risk_only=false`; migrations run
twice cleanly on an existing DB.

---

## WS3 — Data layer: chains + risk-free rate

### `data/loaders/yfinance_options.py`

`yf.Ticker(sym).options` → expiries; `.option_chain(expiry)` → calls/puts frames
(`strike, lastPrice, bid, ask, volume, openInterest, impliedVolatility, contractSymbol`,
`lastTradeDate`). **yfinance provides IV but no Greeks** — Greeks always computed locally
(WS1); `model_iv` from mid when provider IV missing/absurd. Trim to near-the-money window
(±20% of spot, expiries inside the DTE window) before persisting.

Chain sanity guard (mirror `data/sanity.py`): drop `bid > ask`, crossed/negative prices,
`ask == 0`; provider IV outside `[0.01, 5.0]` → `implied_vol=None`.

Fallback chain: `yfinance → (none)`. No free secondary chain source — on failure return
`None`; orchestrator skips options for the run (stock pipeline proceeds; note recorded). Never
fabricate a synthetic chain in decision mode. When Stage 2/3 is live, WS14's MCP connector may
be probed as a quote-quality cross-check for the specific contracts being traded (execution
uses venue quotes at order time regardless).

### `data/loaders/rates.py`

Risk-free: `^IRX` (13-week T-bill) → continuous (`r = ln(1 + y)`); fallback
`settings.options.default_risk_free_rate` (0.04) with provenance note. Dividend yield from the
existing fundamentals path, fallback 0. `DiskCache`, 1-day TTL.

### Router / snapshot integration

`DataRouter.get_option_chain(underlying, as_of) -> OptionChainSnapshot | None`;
`build_snapshot` populates `RunState.option_chain` when `mandate.options.enabled` and the
symbol is in `underlying_universe`. Compute `atm_iv` (nearest-expiry ATM straddle IV average),
`iv_rank`/`percentile` from `iv_history`, `rv_yang_zhang` from OHLCV; append today's
`iv_history` row.

Acceptance: fixture-frame loader tests (no network); sanity-guard tests; graceful `None` on
provider failure; `pricing_source == "live_chain"` always in decision mode.

---

## WS4 — Risk: gate, sizing, collateral

### Sizing (`risk/sizing.py` addition)

Options sized by **max loss**, not notional:

```
contracts = floor( min(mandate.max_loss_per_position_usd,
                       equity × pm_scale × conviction_scaled_budget)
                   / max_loss_per_1_spread )
```

Reuse `pm_scale` (PM can only shrink). 0 contracts if `max_loss_per_1_spread ≤ 0`.

### Gate (`risk/gate.py`) — new `check_option_order(...)`

Same audit/emit pattern as `check_order`; shares general rules (kill switch, cooldown on
underlying, per-day caps, daily-loss halt, universe). Additional deterministic checks:

* `OPTIONS_DISABLED` when `mandate.options.enabled` false.
* `NAKED_SHORT_OPTION`: structural check — net-short call legs without 100-share coverage per
  contract, or net-short puts without full cash collateral or a protective long put below →
  veto. Computed from net leg exposure after the order vs current `OptionPosition`s + stock
  positions, **never** from the strategy label.
* `MAX_LOSS_EXCEEDED`: recompute max loss from legs (WS1 — never trust the proposal's copy).
* `OPTIONS_BUDGET_EXCEEDED`: Σ open max_loss + this order vs cap × equity.
* `INSUFFICIENT_COLLATERAL`: CSP `strike×100×contracts − credit ≤ cash`; credit spread
  `width×100×contracts − credit ≤ cash`.
* `ILLIQUID_CONTRACT`, `DTE_OUT_OF_RANGE`, `EXPIRY_TOO_CLOSE` (≤ 2 trading days),
  `GREEKS_CAP_EXCEEDED` (net |Δ|, |vega| after order vs caps), `UNDERLYING_NOT_ALLOWED`.

Collateral model: escrow `max_loss` (spread margin / CSP strike-cash) via an
`available_cash(portfolio, option_positions)` helper used by gate + apply-fill; `Portfolio`
equity semantics unchanged.

Acceptance: per-violation unit tests (pass + fail per code); a structural test that a naked
call mislabeled `covered_call` is vetoed; audit-ledger assertion including legs.

---

## WS5 — Paper execution & option lifecycle

### PaperBroker option fills

`submit()` accepts option orders: atomic multi-leg fill at combined mid ± slippage (pay 25% of
each leg's half-spread, floor $0.01/leg, direction-aware) + `commission_per_contract`
(default $0.65). Reject stale legs (>30 min in market hours), missing bid/ask, inconsistent
type/legs. Emits existing `OrderSubmitted`/`OrderFilled` events.

### Portfolio accounting (`execution/portfolio.py`)

`apply_option_fill(portfolio, option_positions, order, fill)`: debits reduce cash by
premium+commission; credits add credit−commission and escrow collateral; closes reverse.
Realized P&L on close = signed premium delta − all commissions. Persist to `option_positions`;
equity snapshots mark options at chain mid (`marked_equity` gains `option_marks`; conservative
fallback: long legs at intrinsic, short legs at last mid — documented).

### PositionMonitor lifecycle (`risk/monitor.py`)

Per tick, per open `OptionPosition`:

1. Re-fetch marks (router; on failure skip tick + note — never fake a mark).
2. **Premium stop/TP**: close when net premium crosses `stop_loss_pct_premium` /
   `take_profit_pct_premium` (`reason="stop_loss"|"take_profit"`).
3. **Time exits**: close at `horizon_days`; force-close **any** position at
   `DTE ≤ force_close_dte` (default 1). Deterministic default: *never carry into expiration.*
4. **Expiry settlement** (only if a close failed): EOD-of-expiry at intrinsic vs underlying
   close — long ITM → cash-settle intrinsic; covered call ITM → shares called away at strike;
   CSP ITM → shares purchased at strike (collateral consumed); spreads → net intrinsic; OTM →
   worthless. Each settlement appends an order (`reason="expiry_settlement"` — extend the
   Literal) + audit entry. **In live mode (Stage 3, future) settlement records what the broker
   reports rather than simulating — WS13 reconciliation owns that.**

Acceptance: per-strategy open→mark→close round-trips (cash ≥ 0 invariant); fake-clock monitor
tests for stop/TP/DTE/each settlement branch; kill switch halts option execution.

---

## WS6 — Agents & prompts

### New `OptionsAnalyst` (5th parallel analyst, quick tier)

Input facts: ATM IV, IV rank/percentile, IV−RV(YZ) spread, term-structure slope across loaded
expiries, put/call OI skew, expected move `spot × atm_iv × √(DTE/365)`, days to next earnings.
Output `OptionsAnalystReport(AgentReport)`: `iv_regime: Literal["cheap","fair","rich"]`,
`expected_move_pct`, `skew_note`, `event_risk: list[str]`, `confidence`. Prompt
`agents/prompts/options_analyst.md`. Skip the LLM call when `option_chain is None` (like
SentimentAnalyst).

### Trader extension (not a new agent)

When candidates exist, the Trader prompt lists WS1.5 candidates with pre-computed economics
(max loss, breakevens, POP proxy, net Greeks, liquidity score) and may return **either**
`TradeProposal` **or** `OptionStrategyProposal` (structured-output union; one repair retry then
`SchemaParseError`). `candidate_id` must match an offered candidate — checked in code; the
Trader never invents strikes/expiries.

### Downstream agents

Risk debaters + PM see the option proposal with deterministic economics (max loss % of equity,
collateral, Greeks headroom) **and, in live mode, the venue + live-mandate headroom** so the PM
reasons about real money explicitly. PM `approved_quantity_pct` scales contracts via WS4.
Reflection: options `JournalEntry` records strategy id + max loss; grading = realized P&L /
max_loss (return on risk) vs SPY; setup tags gain `iv_rank:high|mid|low`, `strategy:<id>`,
`venue:<paper|live>` so lessons recall by structure and don't cross-contaminate paper/live
performance.

Acceptance: FakeLLM end-to-end option run; schema-repair test; candidate-id-mismatch rejection;
empty-chain skip test.

---

## WS7 — Orchestrator integration

* `_fetch_data`: chain fetch in `build_snapshot` (non-fatal on failure).
* `_analysts`: 5-way gather with OptionsAnalyst.
* Candidate building **in code** between `research_manager` and `trader`; stored on state.
* `_mandate_gate`/`_execute`: branch on proposal type → `check_option_order` + option sizing →
  atomic option order → **ExecutionRouter** (WS11) instead of direct `self.broker`.
* Checkpoint/resume serializes new state fields; resume mid-`risk_debate` reconstructs
  candidates from state (no re-fetch).
* Cost budget: option runs add one quick-tier call; assert a full option run stays < $0.50 in
  the FakeLLM cost roll-up test.

Acceptance: resume-through-option-path test; kill-switch mid-run; artifacts include
`chain.parquet` + candidates JSON.

---

## WS8 — Backtesting: synthetic options seam

No free historical chain data exists, so backtests **synthesize** option prices (the
QuantConnect/WealthLab approach), labeled `pricing_source="synthetic_bsm"` everywhere.

* `SyntheticChainProvider`: from OHLCV ≤ t (anti-lookahead), chain at model prices with
  `σ_t = YangZhang(21d) × iv_premium_factor` (default 1.1 — options trade ~1 vol pt over RV;
  documented), listed-strike grid, monthly expiries, `r` from `^IRX` history or constant,
  synthetic half-spread (default 2% of premium, min $0.05).
* Rule-mode: add `covered_call_overwrite` and `csp_wheel` `Strategy` implementations
  (exercise the full options accounting stack without LLM spend).
* Agent-mode: existing orchestrator seam; decide t, execute t+1 synthetic open-chain
  (anti-lookahead sentinel test for chains).
* Metrics: existing `BacktestResult` on the equity curve; add `premium_captured`,
  `assignments`, `win_rate_by_strategy` into `trades` dicts.
* **Live-relevance rule:** backtest results rendered anywhere near live-mode UI carry
  "synthetic pricing — not indicative of live fills."

Acceptance: determinism (two identical runs); chain anti-lookahead sentinel; hand-checkable
covered-call premium capture on a fixture; CRR-vs-BSM consistency.

---

## WS9 — TUI & export

* **Portfolio screen**: option positions table (strategy, legs, DTE, net premium, mark,
  unrealized P&L, max loss, collateral, **venue badge**) + portfolio net Greeks row; live vs
  paper sections visually separated (live = distinct color + "LIVE" badge).
* **Run Monitor**: OptionsAnalyst card; candidates table with chosen candidate highlighted;
  gate panel shows option + live violation codes; venue banner per run.
* **New "Chain" modal**: near-the-money chain with local Greeks, IV rank header, provenance tag.
* **Live panel** (WS13 data): working orders, partial fills, last reconciliation status/time,
  RH agentic account cash/funding-cap headroom, one-key jump to kill switch.
* **History/export**: option + live orders/positions/journal columns; CSV round-trips; every
  export row carries `venue`.

Acceptance: Textual Pilot tests for new tables/modal/live panel; export golden-file test.

---

## WS10 — Docs & product

* README (currently missing) covering: what Sentinel is, the graduation ladder, options
  feature, synthetic-backtest caveat, and the **research-use-only / not-financial-advice
  disclaimer plus a live-trading warning** ("you are giving software authority over real money;
  losses are yours"). Disclaimer also in TUI footer + CLI `--help` epilog + `sentinel graduate`
  confirmation flow.
* `docs/OPTIONS.md`: math notes, mandate knobs, strategy menu, settlement rules.
* `docs/LIVE.md`: graduation ladder, Robinhood setup (crypto API keys, agentic account linking,
  funding cap guidance = `max_account_allocation_usd`), reconciliation semantics, incident
  playbook (kill switch → cancel-all → reconcile → post-mortem).
* `docs/DECISIONS.md` entries: official-rails-only (unofficial APIs rejected); BSM+CRR local
  Greeks; Brent IV; defined-risk-only; force-close-before-expiry; synthetic backtest pricing;
  broker-as-source-of-truth; limit-orders-only default in live mode.
* `sentinel doctor`: chain fetch check, `^IRX` reachable, options tables migrated, per-stage
  live readiness (keys present, signing works via a read-only authenticated call, MCP server
  reachable, agentic account linked, funding cap ≥ configured allocation), kill-switch path
  writable.

---

## WS11 — ExecutionRouter & broker protocol (v2.0, gates all live work)

`execution/router.py`:

```python
class ExecutionRouter:
    """Route each gated order to the broker for its (asset_class, stage)."""
    def __init__(self, paper: PaperBroker,
                 crypto: RobinhoodCryptoBroker | None,
                 agentic: RobinhoodAgenticBroker | None,
                 live_mandate: LiveMandate, audit=...): ...
    async def submit(self, order: Order) -> Fill | OrderRejected | OrderPending
```

* Routing is **pure function of config + order.asset_type**: stage flag off → paper. No
  heuristics, no fallthrough from live to paper on error (a failed live order is a failed
  order — never silently re-execute on paper).
* Introduces `OrderPending` (broker accepted, fill async — live rails are not synchronous like
  PaperBroker). Orchestrator `_execute` handles it: persist as `live_orders` row with
  `client_order_id`, emit `OrderSubmitted`, and hand tracking to WS13; the run completes with
  status noting a working order (run does not block on fill).
* `Broker` protocol grows `cancel(client_order_id)`, `open_orders()`, `positions()`,
  `capabilities() -> set[str]` (e.g. `{"equity"}`, `{"crypto"}`, later `{"option"}`). Router
  rejects with `VENUE_CAPABILITY_MISSING` when an order's asset type isn't in the target
  venue's capabilities — this is how Stage 3 stays safely dormant until RH ships options.
* Every live submit/cancel/ack appends to the fsync'd audit ledger with full request/response
  metadata (keys redacted).

Acceptance: routing matrix test (asset × stage → venue); no-fallthrough test; capability
rejection test; `OrderPending` persistence test.

---

## WS12 — Live safety: mandate overlay, graduation, secrets, kill-cancel (v2.0)

`risk/live_mandate.py` — `check_live_order(order, ...)` runs **in addition to** the normal
gate, only for live-routed orders:

* `LIVE_STAGE_NOT_ENABLED`, `LIVE_NOTIONAL_EXCEEDED` (vs `max_live_order_notional_usd`),
  `LIVE_ORDER_LIMIT`, `QUOTE_TOO_OLD` (venue quote ≤ `quote_max_age_seconds`),
  `LIVE_DAILY_LOSS_HALT` — computed from **broker-reported** realized+unrealized day P&L
  (reconciliation data, WS13), not Sentinel's mirror. If reconciliation data is stale
  (> 2 ticks), live orders are vetoed outright (`RECONCILIATION_MISMATCH`).
* `require_limit_orders`: live market orders vetoed by default; the router converts sized
  intents to marketable limits (crypto: best ask × (1+bps buffer); equities: RH trade-preview
  price when the MCP exposes it) — buffer configurable, default 10 bps.
* Total allocation check: live positions' notional + order ≤ `max_account_allocation_usd`.

`sentinel graduate <crypto|equity|options>` (CLI + confirm flow):
checks the ladder prerequisites (§2) deterministically against SQLite history + doctor probes,
prints exactly what will change, requires the typed phrase, writes the stage flag to
`mandate.toml` **with an audit entry**, and refuses if the kill switch is engaged. `sentinel
demote <stage>` is instant, no confirmation (de-risking must always be one command).

Secrets: crypto API key + Ed25519 private key and MCP credentials via env (`.env`) only —
never in `config.toml`, never logged, redacted in doctor output (existing pattern). Add a
`sentinel doctor --live` section that performs one signed read-only call per enabled rail.

Kill switch upgrade: engaging KILL now also (a) cancels all open live orders on every enabled
rail, (b) emits `KillEngaged` with per-venue cancel results, (c) blocks `graduate`. A
`--flatten` variant additionally market-closes live positions (typed confirmation; the one
permitted live market order).

Acceptance: overlay per-violation tests; graduation prerequisite matrix test (each unmet
prerequisite blocks); kill-cancel test against a fake broker asserting cancels issued per
venue; secrets-redaction test.

---

## WS13 — Reconciliation & live order lifecycle (v2.0)

`execution/reconcile.py`, running inside the existing background-services loop
(`service.start_background_services`), per enabled rail:

1. **Order tracking**: poll `open_orders()` (crypto REST; agentic MCP) at the monitor cadence;
   update `live_orders` status (`working → partial → filled|cancelled|rejected`); on fill(s),
   record `Fill`(s) with `broker_order_id`, apply to portfolio accounting, emit `OrderFilled` /
   `EquityUpdated`. Partial fills apply incrementally; a working order past
   `live_order_ttl_minutes` (default 30) is cancelled and audited.
2. **Position/cash reconciliation**: fetch broker positions + cash; diff against Sentinel's
   mirror (qty per symbol, cash within $0.01, contracts per option position when Stage 3
   exists). Match → `reconciliations(ok=true)`. Mismatch → `reconciliations(ok=false,
   diff_json)`, emit `ReconciliationFailed`, **halt new live risk** (live gate vetoes until a
   clean pass), TUI banner + push path (see below). Manual `sentinel reconcile --adopt-broker`
   resolves by adopting broker state (audited); never auto-adopt.
3. **Idempotency**: every live submit carries `client_order_id` (ULID). On submit timeout /
   crash-resume, query by `client_order_id` before ever re-submitting. Crash-safe: on startup,
   reconcile before the first run of the day is allowed to execute live.
4. **Notifications**: Robinhood pushes its own trade notifications (agentic accounts);
   Sentinel additionally logs every live event to the audit ledger and surfaces a TUI alert
   feed. (Email/IM remains a non-goal.)

Acceptance: fake-broker lifecycle tests (accept → partial → fill; accept → TTL cancel;
submit-timeout → idempotent re-query not re-submit); mismatch-halts-live test; crash-resume
reconcile-first test.

---

## WS14 — Robinhood connectors (v2.0)

### WS14.1 `execution/robinhood_crypto.py` — Stage 1 (wire the existing scaffold)

Extend the inert scaffold (`execution/robinhood.py`) into a working client on the **official
Crypto Trading API**: Ed25519 signing (already implemented in `_sign_headers`), endpoints for
best bid/ask, holdings, order submit/status/cancel. Market + limit; Sentinel defaults to
marketable limits (WS12). Keep the `enabled=false`, `crypto_only=true`, allowed-symbols ∩
{BTC-USD, ETH-USD} guards. Respect documented rate limits with client-side throttling +
`ProviderRateLimited`-style backoff; classify errors (auth vs validation vs transient) —
transient retries are bounded and idempotent via order lookup, never blind re-submit.
`capabilities() == {"crypto"}`.

### WS14.2 `execution/robinhood_agentic.py` — Stage 2/3 (MCP client)

Client for **Robinhood's Agentic Trading MCP servers** (beta): dedicated agentic account;
equities now, options later.

* Transport: MCP client session (stdio/HTTP per RH docs at
  robinhood.com/us/en/support/agentic-trading/); auth per RH's onboarding (account linking is
  a manual user step documented in `docs/LIVE.md`).
* Map Sentinel orders → MCP trade tools; **always use RH's trade-preview before submit** and
  persist the preview (est. price/cost) in the audit entry; if the user enabled RH-side manual
  approval, `OrderPending` remains working until the user approves in-app (WS13 tracks it).
* `capabilities()` derived from a **runtime probe** of the MCP server's advertised tools —
  equities today; the day RH ships options tools, the probe flips and Stage 3 becomes
  *possible* (still requires WS12 graduation). No code change should be needed beyond mapping
  multi-leg orders, which is written now behind the capability flag and integration-tested
  against a fake MCP server.
* The MCP server is an **execution dependency only** — analysts/data continue to use the
  existing data layer; RH account data (cash, positions, orders) flows only into
  reconciliation and the live panel.

Acceptance: fake-MCP-server integration tests (list tools → probe capabilities; preview →
submit → fill event; approval-pending flow; cancel); signing golden test for crypto (fixed
timestamp/key → exact header values); zero live tests in CI (all fakes); a documented manual
smoke checklist in `docs/LIVE.md` for the user's first real $10 trade.

---

## 5. Configuration summary

```toml
[options]
enabled = false
commission_per_contract_usd = 0.65    # paper modeling; live fees come from broker reports
slippage_half_spread_frac = 0.25
force_close_dte = 1
default_risk_free_rate = 0.04

[backtest.options]
iv_premium_factor = 1.1
synthetic_half_spread_pct = 2.0

[live]                                # v2.0; stage flags live in mandate.toml (§WS2)
live_order_ttl_minutes = 30
marketable_limit_buffer_bps = 10
reconcile_interval_min = 5

[execution.robinhood]                 # existing section, extended
enabled = false                       # crypto rail master switch
crypto_only = true
[execution.robinhood_agentic]
enabled = false                       # equities rail master switch
mcp_endpoint_env = "RH_AGENTIC_MCP_URL"
```

## 6. Global acceptance (orchestrator's definition of done)

1. Existing 117 tests green; new suites ≥ 90 tests; coverage ≥ 85% on risk/execution/options.
2. Default config (all stages off, options off) → behavior byte-identical to today.
3. **Paper options story test** (FakeLLM, one test): bullish plan → bull call spread candidate
   → PM approve → gate pass → atomic paper fill → premium-stop close → journal + reflection
   with return-on-risk grading.
4. **Live crypto story test** (fake broker): gated order → live overlay pass → marketable
   limit via router → `OrderPending` → reconcile-driven fill → equity event → broker/mirror
   reconciliation clean. Then: kill switch → open order cancelled.
5. Gate + overlay cannot be bypassed: no code path reaches a live rail without both passing;
   a naked short structurally cannot fill anywhere; no-fallthrough (live failure never
   auto-executes on paper).
6. Reconciliation mismatch halts live risk and is visible in the TUI within one tick.
7. A live decision run on SPY (manual, documented in PR) completes < 4 min, < $0.50, chain
   provenance `live_chain`.
8. Every synthetic-priced artifact labeled; every live artifact carries venue + broker ids.
9. README + `docs/LIVE.md` + disclaimers shipped; `sentinel doctor --live` accurately reports
   per-stage readiness; `sentinel demote` works with no confirmation.

## 7. Math reference — golden values for test authors

`S=100, K=100, T=1.0, r=0.05, q=0.0, σ=0.20`:
`d1=0.35, d2=0.15, N(d1)=0.63683, N(d2)=0.55962` →
call `10.4506`, put `5.5735` (parity: `10.4506 − 5.5735 = 100 − 95.1229 = 4.8771` ✓),
Γ `0.018762`, vega `37.524` per 1.00 (`0.37524`/pt), call θ `−6.414`/yr (`−0.01757`/day),
call ρ `53.232` per 1.00. American put (CRR, 200 steps) ≈ `6.090` > European.
Crypto-signing golden: fixed API key/private key/timestamp → assert exact
`x-api-key`/`x-signature`/`x-timestamp` header values (extend the existing scaffold test).
Sources of record: Hull, *Options, Futures and Other Derivatives*; je-suis-tm/quant-trading
(straddle construction; VIX-style variance methodology — optional v1.1 analytics); yfinance
`option_chain()` field semantics; Robinhood Crypto Trading API docs (docs.robinhood.com);
Robinhood Agentic Trading announcement + support docs (May 2026).

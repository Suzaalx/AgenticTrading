# Sentinel

**Sentinel** is a personal, terminal-first agentic trading system. It runs an 11-node multi-agent pipeline to analyze stocks and options, debate risk, make trade decisions, and execute them through Robinhood's Agentic Trading MCP — all from your terminal.

> **Research use only. Not financial advice.** If you enable live trading, you are giving software authority over real money in your own Robinhood account. Losses are yours. Start in paper mode. Start tiny.

---

## How it works

```
9:30 AM every weekday
    → fetch price data + news (Alpha Vantage + Finnhub)
    → 5 analysts run in parallel (momentum, fundamentals, sentiment, technical, options)
    → bull/bear debate → research manager judges thesis
    → trader proposes action
    → risk debate (3 agents) → portfolio manager verdict
    → mandate gate (deterministic hard limits)
    → execute → paper broker OR Robinhood Agentic MCP
    → journal outcome → reflect → store lesson for next run
```

Every decision, debate, report, fill, and lesson is stored in a local SQLite database and visible in the terminal UI.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.12+ | Check: `python3 --version` |
| `uv` package manager | Install below |
| Anthropic API key | console.anthropic.com — pay per token (~$0.30/run) |
| Alpha Vantage key | alphavantage.co — free tier |
| Finnhub key | finnhub.io — free tier |
| Robinhood account | For live trading only — paper mode needs none |

---

## Setup (step by step)

### Step 1 — Clone the repo

```bash
git clone https://github.com/Suzaalx/AgenticTrading.git
cd AgenticTrading
```

### Step 2 — Install `uv` (package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then reload your shell or add to PATH:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Step 3 — Install dependencies

```bash
uv sync
```

### Step 4 — Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in your API keys:

```env
ANTHROPIC_API_KEY=sk-ant-...        # console.anthropic.com → API Keys
ALPHA_VANTAGE_KEY=...               # alphavantage.co/support/#api-key (free)
FINNHUB_KEY=...                     # finnhub.io/register (free)
RH_AGENTIC_MCP_URL=https://agent.robinhood.com/mcp/trading
RH_AGENTIC_MCP_TOKEN=               # leave blank — not needed with Claude Code bridge
```

### Step 5 — Verify setup

```bash
uv run sentinel doctor
```

You should see all keys present, DB writable, kill switch path writable.

### Step 6 — Launch the terminal UI

```bash
uv run sentinel
```

**Mac keyboard tip:** The TUI uses F1–F8 keys. On Mac, hold **Fn** while pressing them, or go to System Settings → Keyboard → enable "Use F1, F2, etc. as standard function keys".

| Key | Tab |
|-----|-----|
| Fn+F1 | Dashboard |
| Fn+F2 | Run (start analysis) |
| Fn+F3 | Portfolio |
| Fn+F4 | History |
| Fn+F5 | Backtest |
| Fn+F6 | Memory (past lessons) |
| Fn+F7 | Logs |
| Fn+F8 | Live Robinhood accounts |
| R | New run |
| K | Kill switch |
| ? | Help |
| Q | Quit |

---

## Running your first paper trade

```bash
uv run sentinel run NVDA
```

Or from inside the TUI: press **R**, enter `NVDA`, pick depth `standard`, press Enter.

This runs the full pipeline in paper mode — no real money, no Robinhood account needed. Results appear in F1 Dashboard and F4 History.

---

## Robinhood live trading setup

Live trading uses Robinhood's Agentic Trading MCP, connected through Claude Code. **No OAuth token extraction needed** — the bridge reuses Claude Code's existing session.

### Step 1 — Connect Robinhood MCP to Claude Code

Run this once in your terminal:

```bash
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
```

Then inside Claude Code, type `/mcp`, select `robinhood-trading`, and authenticate with your Robinhood account.

> Agentic trading access is gradually rolling out. You'll receive an email from Robinhood when your account gets access.

### Step 2 — Fund your Agentic account

Robinhood creates a separate **Agentic account** during the MCP setup. Sentinel can only place orders in this account (not your main account).

- Transfer funds into it from your Robinhood app
- Minimum recommended: **$500** (options spreads require ~$50–100 per trade)

### Step 3 — Enable options on the Agentic account

Visit this URL while logged into Robinhood to upgrade options level:

```
https://applink.robinhood.com/upgrade_options?account_number=<YOUR_AGENTIC_ACCOUNT_NUMBER>
```

Your agentic account number is shown in the F8 Live RH tab inside the TUI.

### Step 4 — Update `config.toml`

```toml
[execution.robinhood_agentic]
enabled = true
agentic_account_number = "YOUR_AGENTIC_ACCOUNT_NUMBER"   # e.g. "501716948"
use_claude_bridge = true

[options]
enabled = true
```

### Step 5 — Update `mandate.toml` risk limits

```toml
[mandate.live]
options_stage_enabled = true          # set true only after 30+ paper days
max_live_order_notional_usd = 100.0   # max per-order notional
max_live_daily_loss_usd = 50.0        # daily loss hard stop
max_live_orders_per_day = 2
max_account_allocation_usd = 500.0    # never deploy more than this
```

### Step 6 — Graduate to live (after paper testing)

Sentinel enforces a prerequisite checklist before allowing live orders:

```bash
uv run sentinel graduate options
```

This checks: 30+ days of paper history, mandate limits set, live stage flags, kill switch path. You must type a confirmation phrase to proceed.

---

## Recommended strategy: SPY weekly put credit spreads

Based on 2026 research data, this is the highest win-rate, lowest-risk strategy for small accounts:

| Property | Value |
|----------|-------|
| Symbol | SPY |
| Strategy | Put credit spread (sell higher put, buy lower put) |
| Delta | 15–20 delta short strike (2–5% below current price) |
| Expiry | 7 DTE (7 days to expiration) |
| Close rule | Close at 50% max profit — don't hold to expiry |
| Win rate | ~80–91% historically |
| Risk per trade | $50–100 (1 contract, $1-wide spread) |
| Target per trade | $10–20 credit collected |

**`config.toml` settings for this strategy:**

```toml
[schedule]
enabled = true
watchlist = ["SPY"]                   # SPY only to start
decision_cron = "30 9 * * 1-5"       # run at 9:30 AM Mon-Fri

[options]
enabled = true
min_dte = 5
max_dte = 14
force_close_dte = 1
```

**`mandate.toml` options settings:**

```toml
[mandate.options]
enabled = true
underlying_universe = ["SPY"]
defined_risk_only = true
max_loss_per_position_usd = 100.0
max_contracts_per_order = 1
min_dte = 5
max_dte = 14
```

---

## API cost management

Sentinel uses Claude (Anthropic API) for its agents. Every pipeline run costs tokens.

| Setting | Location | Purpose |
|---------|----------|---------|
| `monthly_budget_usd` | `config.toml [llm]` | Hard cap on monthly Anthropic API spend |
| `deep_model` | `config.toml [llm]` | Model for trader, PM, research manager |
| `quick_model` | `config.toml [llm]` | Model for 5 analysts |

**Recommended budget config:**

```toml
[llm]
monthly_budget_usd = 15.0
deep_model = "claude-opus-4-7"
quick_model = "claude-sonnet-4-6"
watchlist = ["SPY"]                   # 2 symbols ≈ $13/month
```

Approximate costs per run:
- 1 symbol, standard depth → ~$0.30
- 2 symbols/day, 22 trading days → ~$13/month

---

## Memory and learning

Sentinel gets smarter over time through a built-in reflection loop:

```
Run → journal entry → sentinel reflect → lesson extracted by AI
→ lesson stored in FTS5 database
→ next run: top 5 relevant lessons injected into every analyst prompt
→ agents factor in past mistakes
```

**Daily reflection (add to crontab):**

```bash
crontab -e
```

```
30 18 * * 1-5 cd /path/to/AgenticTrading && uv run sentinel reflect >> ~/sentinel_reflect.log 2>&1
```

**Memory commands:**

```bash
uv run sentinel reflect                    # extract lessons from today's outcomes
uv run sentinel memory list                # see all stored lessons
uv run sentinel memory search "SPY IV"     # search lessons by topic
uv run sentinel memory forget <lesson_id>  # remove a bad lesson
```

After 30 days of paper trading: ~40–50 lessons learned. After 90 days: robust pattern library for your specific symbols and market conditions.

---

## Kill switch

Press **K** in the TUI at any time to arm/disarm the kill switch. When armed, all live order submission is blocked. From the CLI:

```bash
uv run sentinel kill on           # engage (blocks all live orders)
uv run sentinel kill on --flatten # engage AND market-close all live positions
uv run sentinel kill off          # disarm
```

---

## All CLI commands

```bash
uv run sentinel                   # launch TUI
uv run sentinel run NVDA          # single paper run
uv run sentinel run SPY --depth deep
uv run sentinel portfolio         # print paper portfolio
uv run sentinel portfolio --live  # print live Robinhood portfolio
uv run sentinel history           # recent decisions
uv run sentinel history --symbol SPY
uv run sentinel reflect           # run reflection job
uv run sentinel memory list       # list stored lessons
uv run sentinel backtest --symbol SPY --strategy sma_cross
uv run sentinel doctor            # readiness check
uv run sentinel doctor --live     # include live rail checks
uv run sentinel graduate options  # promote options to live
uv run sentinel demote options    # demote options back to paper
uv run sentinel kill on/off       # kill switch
uv run sentinel snapshot NVDA     # data snapshot for debugging
uv run sentinel export <run_id>   # export run bundle to markdown
```

---

## Project structure

```
sentinel/
├── agents/          # analyst, trader, risk, portfolio manager, reflection agents
├── config/          # settings and mandate loading
├── core/            # event bus, models, pipeline state
├── data/            # Alpha Vantage, Finnhub, yFinance loaders
├── execution/       # paper broker, Robinhood MCP bridge, router
│   ├── claude_mcp_bridge.py   # routes orders through Claude Code's Robinhood session
│   ├── robinhood_agentic.py   # live broker for equities and options
│   └── rh_viewer.py           # read-only live portfolio viewer
├── llm/             # Anthropic/OpenAI gateway, budget tracking
├── memory/          # journal, reflection, FTS5 lesson store
├── orchestrator/    # 11-node pipeline graph, runner, command service
├── risk/            # kill switch, audit log, mandate gate
├── store/           # SQLite migrations, export
└── tui/             # Textual terminal UI (F1–F8 tabs)

config.toml          # LLM, pipeline, data, execution, options settings
mandate.toml         # risk limits, universe, live stage flags
.env                 # API keys (never commit this)
```

---

## Troubleshooting

**`ANTHROPIC_API_KEY is required`**
→ Add your key to `.env` and re-run.

**`uv not found`**
→ Run `export PATH="$HOME/.local/bin:$PATH"` or restart your terminal after installing uv.

**TUI shows blank panels**
→ No data yet. Run `uv run sentinel run SPY` first to generate a decision, then reopen the TUI.

**F-keys trigger Mac system functions**
→ Hold **Fn** while pressing F1–F8, or enable standard function keys in System Settings → Keyboard.

**Live RH tab shows "token not set"**
→ Connect Robinhood MCP to Claude Code: `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`, then `/mcp` → authenticate.

**`graduation refused`**
→ Run `uv run sentinel doctor --live` to see which prerequisites are missing.

---

## Disclaimer

Sentinel is experimental software for personal research use. The agents can make errors, misinterpret data, and generate losing trade ideas. You are responsible for every trade placed in your account. Past paper performance does not guarantee live performance. Options involve significant risk and can result in total loss of the amount invested. Only trade with money you can afford to lose entirely.

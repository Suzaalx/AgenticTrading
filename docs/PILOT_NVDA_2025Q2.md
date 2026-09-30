# Pilot comparison — NVDA, 2025-03-03 → 2025-05-30

Purpose: validate the shared evaluation harness end-to-end on one symbol and one fixed
window before scaling up. Compares `sentinel_debate_on`, `sentinel_debate_off`,
`sma_cross` and `buy_hold` on identical bars with identical fills.

## Setup

| item | value |
|---|---|
| symbol / window | NVDA, 2025-03-03 → 2025-05-30 (63 daily bars, yfinance via the data router, cached immutably) |
| fill model | decision on bar *t* → fill at open of *t+1*, slippage 5 bps, commission $0, starting cash $10,000 |
| agent cadence | weekly (every 5th bar → 13 Sentinel decisions per variant); rule baselines decide every bar |
| sma_cross params | short 5 / long 20 (the 20/50 default never warms up inside a 63-bar window) |
| LLM backend | Groq free tier (`provider = "groq"`): `openai/gpt-oss-20b` quick, `openai/gpt-oss-120b` deep, `analyst_history_bars = 25`, `max_tokens = 2048`, `max_position_pct_equity = 10` |
| experiment id | `pilot_nvda_2025q2` → `~/.sentinel/experiments/pilot_nvda_2025q2/{results.csv,experiment.db}` |

## Commands

```bash
# rule baselines (no LLM; already run — rows below)
uv run sentinel eval run -p sma_cross -p buy_hold -s NVDA --start 2025-03-03 --end 2025-05-30 \
    --short-window 5 --long-window 20 --experiment pilot_nvda_2025q2

# Sentinel rows (needs GROQ_API_KEY in .env). Same experiment id => rows are added to the same table.
# --resume keeps completed decisions and only re-queries failed/missing ones (daily token cap).
uv run sentinel eval run -p sentinel_debate_on -p sentinel_debate_off -s NVDA --start 2025-03-03 --end 2025-05-30 \
    --history-bars 25 --experiment pilot_nvda_2025q2 --resume

# rebuild the combined table at any time
uv run sentinel eval report --experiment pilot_nvda_2025q2
```

## Results

### Rule baselines (real NVDA data)

```
pipeline   symbol  window                     ret  sharpe    maxDD   win%  trades/yr  decisions  tok/dec   $/dec  llm_ms/dec  wall_ms/dec  failed
sma_cross  NVDA    2025-03-03..2025-05-30  25.20%    4.04   -3.16%  0.00%       16.0         63        0  0.0000           0            0       0
buy_hold   NVDA    2025-03-03..2025-05-30  22.06%    1.56  -22.49%    n/a        4.0         63        0  0.0000           0            0       0
```

### Sentinel debate-on / debate-off

**Incomplete — not reportable yet.** Attempt 2 (2026-09-29, 18:30–21:20 UTC) ran into Groq's free-tier
**daily** cap of 200k tokens per model:

| variant | decisions completed | failures |
|---|---|---|
| `sentinel_debate_on` | 9 / 13 | 3 × HTTP 429 "tokens per day", 1 × `SchemaParseError` (trader omitted `quantity_pct`) |
| `sentinel_debate_off` | 0 / 13 | 13 × HTTP 429 "tokens per day" (allowance already spent by debate-on) |

Failed decisions become HOLD, so the stored rows (debate-on +1.16%, debate-off 0.00%) are biased toward
inaction and must not be read as results. Cost side from the 9 completed debate-on decisions: ~25.5k
tokens per decision (~17.4k on gpt-oss-20b, ~8.1k on gpt-oss-120b), $0, ~13 s mean model time per call.

Remaining work: 4 debate-on + 13 debate-off decisions ≈ 190k tokens on gpt-oss-20b — at the edge of one
day's allowance, so it may need two `--resume` passes. Run the command above once the rolling window has
freed (≈ 21:30 UTC the next day).

### Harness validation dry-run (real NVDA bars, `FakeLLM` standing in for Groq)

Run to prove the plumbing on real data without spending free-tier quota. The FakeLLM
always proposes BUY 50% / PM APPROVE, so the Sentinel numbers here are meaningless as a
strategy — only the mechanics matter.

```
pipeline             symbol  window                     ret  sharpe    maxDD   win%  trades/yr  decisions  tok/dec   $/dec  llm_ms/dec  wall_ms/dec  failed
sentinel_debate_on   NVDA    2025-03-03..2025-05-30   1.19%    1.46   -1.27%    n/a       52.0         13        0  0.0000           0           25       0
sentinel_debate_off  NVDA    2025-03-03..2025-05-30   1.19%    1.46   -1.27%    n/a       52.0         13        0  0.0000           0           19       0
sma_cross            NVDA    2025-03-03..2025-05-30  25.20%    4.04   -3.16%  0.00%       16.0         63        0  0.0000           0            0       0
buy_hold             NVDA    2025-03-03..2025-05-30  22.06%    1.56  -22.49%    n/a        4.0         63        0  0.0000           0            0       0
```

Checks that passed: 13 decisions per Sentinel variant (weekly cadence over 63 bars), every
decision metered in `costs` under `<bt_id>:<date>`, `debate_off` made zero
bull/bear/research-manager calls, both rule rows identical to the standalone baseline run,
`sentinel eval report` rebuilds the table from `experiment.db`, re-running an id replaces
rows without double-counting costs.

## What broke / looked off (and what was done about it)

1. **Market-analyst prompt is ~7.5k tokens** (90 bars of OHLCV + 90 bars of indicators).
   Groq's free tier caps `llama-3.1-8b-instant` at 6k tokens *per minute* and the 70B at
   12k TPM / 100k per day, so a single analyst call would be rejected. Added
   `[pipeline] analyst_history_bars` (default 90, unchanged) and `--history-bars` on
   `sentinel eval run`; 30 bars ≈ 2.5k tokens. Recorded in each Sentinel row's `config`.
2. **Rate limiting.** The gateway retried transport errors with ≤1s sleeps and only 3 times,
   which would have turned every 429 into a failed decision (HOLD). It now honours
   `Retry-After` (fallback exponential, ≤90s, 6 attempts) without consuming the transport
   retry budget.
3. **Daily token budget.** _(Superseded by 10: the binding limit turned out to be 200k tokens/day per model on the gpt-oss defaults.)_ 13 decisions × 4 deep-tier calls × ~3–4k tokens
   ≈ 200k tokens/day > Groq's 100k TPD for `llama-3.3-70b-versatile`. Plan for the pilot:
   run everything on `llama-3.1-8b-instant` (`--model llama-3.1-8b-instant`) or split the
   two Sentinel variants across two days. `--model` is recorded in the row config so it is
   obvious which model produced which row.
4. **Exposure dominates return comparisons.** Sentinel sizes each BUY as
   `equity × max_position_pct_equity (10%) × quantity_pct × PM scale` — with the fixture's
   50%/50% that is 5% of equity per decision, versus buy-and-hold at 100%. Added
   `--max-position-pct` (e.g. `100`) so the ablation can be run at comparable exposure, and
   `exposure_pct` is in every row. Judge Sentinel vs baselines on Sharpe / drawdown /
   return-per-exposure, not raw return, unless exposure is equalised.
5. **`sma_cross` shows `win% 0.00%` with `+25.2%` return.** Not a bug: the only *closed*
   round-trip lost, and the open position carrying the gain is marked in equity but is not
   a closed trade. `win_rate` is over closed round-trips only; `buy_hold` never closes, so
   it reads `n/a`. `trade_frequency` counts non-HOLD signals per trading-year, so
   `buy_hold` shows 4.0 (one BUY over 63 bars) — read alongside `round_trips`.
6. **`results.csv` only held the rows from the latest invocation**, which would have
   dropped the baseline rows when the Sentinel rows were added later. Fixed: the CSV is
   now rebuilt from every stored row of the experiment.
7. **Backtest snapshots carry no news/fundamentals** (`news=[]`, `fundamentals=None`), so
   in agent replay the sentiment analyst short-circuits to neutral (no LLM call) and the
   news/fundamentals analysts reason over "no data". Sentinel is therefore evaluated on
   price/indicator information only in this harness — an honest limitation to state next
   to any result.
8. **Trader/PM see the experiment DB's portfolio (always flat)**, not the engine's
   simulated position, so their prompts never know they are already long. Acceptable for a
   pilot; wiring the engine's `BarContext` position into the snapshot is the obvious next
   step if position-aware decisions matter.
9. Pre-existing, unrelated: 3 tests fail on `master` (2 TUI tab-count tests after the
   `live_rh` tab was added; 1 options test whose fixture quote is "stale" relative to
   today's date). `ruff` also reports 12 errors in the last commit's Robinhood files,
   including an undefined `_exchange_code` in `execution/rh_auth.py`. Left untouched
   (live-path code).

10. **Daily token cap (200k tokens/day per model).** Debate-on alone used ~180k on gpt-oss-20b, so
    both variants cannot run in one day. Switching models between variants would confound the ablation,
    so instead `--resume` (commit f019151) rebuilds completed decisions from stored `reports` rows,
    keeps their cost rows, drops failed/partial ones, and re-queries only those.
11. **Hung request (attempt 1, 2026-09-21).** One Groq response never arrived; the OpenAI SDK's 600 s
    timeout plus its own retries, under gateway retries, stalled the run ~1 h at decision 9. Fixed in
    1b674d6 (`request_timeout_seconds = 120`, SDK retries off). Attempt 1 was discarded.
12. **Malformed structured output.** One trader reply omitted `quantity_pct` twice (initial + validation
    retry). Worth tracking as a per-model reliability metric in the full study.

## Run configuration used for the Sentinel rows

Provider groq; quick `openai/gpt-oss-20b`, deep `openai/gpt-oss-120b` (trader, research manager, PM);
`analyst_history_bars = 25`; `max_tokens = 2048`; `max_debate_rounds = 2`; `max_position_pct_equity = 10`;
weekly cadence. Logs: `~/.sentinel/experiments/pilot_nvda_2025q2/sentinel_rows.log` (attempt 2) and
`sentinel_rows.attempt1.log`. Final combined table to be added after the `--resume` pass.

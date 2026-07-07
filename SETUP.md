# Setup — running Sentinel on a new machine

Sentinel is a personal, terminal-first agentic trading system (paper-trading by default;
live Robinhood rails are gated OFF). This guide gets it running from a fresh clone.

See `README.md` for what it does, and `docs/OPTIONS.md` / `docs/LIVE.md` for the options and
live-trading details.

## 1. Prerequisites

- **git**
- **[uv](https://docs.astral.sh/uv/)** (Astral) — manages Python + dependencies. Install it:
  - **Windows (PowerShell):** `irm https://astral.sh/uv/install.ps1 | iex`
  - **macOS / Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Python 3.12+ — `uv` will install it for you if needed (`uv python install 3.12`).

You do **not** need to create a virtualenv by hand — `uv sync` does it. The `.venv/` is
intentionally not committed.

## 2. Clone

```bash
git clone https://github.com/Suzaalx/AgenticTrading.git
cd AgenticTrading
```

(The repo is private — sign in when git/gh prompts, e.g. `gh auth login` first.)

## 3. Install dependencies

```bash
uv sync
```

This creates `.venv/` and installs everything pinned in `uv.lock`.

## 4. Verify it works

```bash
uv run pytest        # full suite (~330 tests) — all should pass, no network needed
uv run sentinel doctor   # readiness report (keys present, DB writable, config valid)
```

## 5. Launch the app

```bash
uv run sentinel          # launches the Textual TUI (Dashboard / Run / Portfolio / ...)
```

Useful headless commands (same engine as the TUI):

```bash
uv run sentinel run NVDA                 # run one decision (needs an LLM key — see below)
uv run sentinel backtest --mode rule --symbol SPY --strategy sma_cross
uv run sentinel portfolio                # show the paper portfolio
uv run sentinel --help                   # all commands
```

## 6. Secrets / API keys (optional, only for real runs)

Data/LLM keys are read from environment / a local `.env` file (never committed). Copy the
template and fill in whatever you have:

```bash
cp .env.example .env
```

Then set any of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPHA_VANTAGE_KEY`, `FINNHUB_KEY`.
Tests and offline/paper flows work without keys.

## 7. Safety note

- **Paper trading is the default and permanent fallback.** No real orders are placed.
- **Live trading is OFF by default** and cannot be turned on by a single switch — it requires
  explicit per-asset "graduation" (`uv run sentinel graduate ...`), a filled live mandate, and
  a typed confirmation. See `docs/LIVE.md`. This is research software, **not financial advice**.

## Working across two computers

```bash
git pull       # get the latest before you start
# ...make changes...
git add -A && git commit -m "your message"
git push       # if push hangs on a locked-down/corporate machine, see README/notes
```

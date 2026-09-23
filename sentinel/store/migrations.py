"""SQLite DDL for Sentinel persistence."""

from __future__ import annotations

DDL_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS runs      (run_id TEXT PRIMARY KEY, symbol TEXT, as_of TEXT, mode TEXT,
                        status TEXT, action TEXT, verdict TEXT, cost_usd REAL,
                        tokens INTEGER, created_at TEXT, finished_at TEXT,
                        debate_enabled INTEGER)""",
    """CREATE TABLE IF NOT EXISTS reports   (run_id TEXT, agent TEXT, model TEXT, content TEXT,
                        structured_json TEXT, tokens_in INT, tokens_out INT,
                        cost_usd REAL, latency_ms INT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS orders    (order_id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, side TEXT,
                        qty TEXT, reason TEXT, status TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS fills     (order_id TEXT, price TEXT, qty TEXT, slippage_usd TEXT,
                        commission_usd TEXT, ts TEXT)""",
    """CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, qty TEXT, avg_cost TEXT, stop_pct REAL,
                        tp_pct REAL, horizon_days INT, opened_at TEXT, source_run_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS equity_curve (ts TEXT, equity REAL, cash REAL, day_pnl REAL)""",
    """CREATE TABLE IF NOT EXISTS journal   (run_id TEXT PRIMARY KEY, horizon_end TEXT, realized_ret REAL,
                        bench_ret REAL, graded TEXT, reflected_at TEXT)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS lessons USING fts5(lesson_id, symbol, setup_tags, what_happened,
                        lesson, grade, created_at)""",
    """CREATE TABLE IF NOT EXISTS backtests (bt_id TEXT PRIMARY KEY, config_json TEXT, metrics_json TEXT,
                        equity_json TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS costs     (ts TEXT, run_id TEXT, agent TEXT, model TEXT, tokens_in INT,
                        tokens_out INT, cost_usd REAL, latency_ms INT)""",
    """CREATE TABLE IF NOT EXISTS option_positions (position_id TEXT PRIMARY KEY, underlying TEXT,
                        strategy TEXT, legs_json TEXT, open_premium TEXT, max_loss TEXT, collateral TEXT,
                        opened_at TEXT, expiry TEXT, horizon_days INT, stop_loss_pct_premium REAL,
                        take_profit_pct_premium REAL, source_run_id TEXT, venue TEXT)""",
    """CREATE TABLE IF NOT EXISTS option_chain_meta (run_id TEXT PRIMARY KEY, underlying TEXT,
                        as_of TEXT, spot TEXT, risk_free_rate REAL, dividend_yield REAL,
                        expiries_json TEXT, atm_iv REAL, iv_rank REAL, iv_percentile REAL,
                        rv_yang_zhang REAL, pricing_source TEXT, providers_json TEXT, chain_path TEXT)""",
    """CREATE TABLE IF NOT EXISTS iv_history (symbol TEXT, date TEXT, atm_iv REAL, rv_yz REAL,
                        PRIMARY KEY (symbol, date))""",
    """CREATE TABLE IF NOT EXISTS live_orders (client_order_id TEXT PRIMARY KEY, broker_order_id TEXT,
                        venue TEXT, status TEXT, submitted_at TEXT, last_sync_at TEXT, raw_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS reconciliations (ts TEXT, venue TEXT, ok INT, diff_json TEXT)""",
)

EXPECTED_TABLES: tuple[str, ...] = (
    "runs",
    "reports",
    "orders",
    "fills",
    "positions",
    "equity_curve",
    "journal",
    "lessons",
    "backtests",
    "costs",
    "option_positions",
    "option_chain_meta",
    "iv_history",
    "live_orders",
    "reconciliations",
)


# Columns added after a table first shipped. Applied idempotently by run_migrations so
# existing ~/.sentinel databases pick them up without a manual migration step.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("costs", "latency_ms", "INT"),
    ("runs", "debate_enabled", "INTEGER"),
)

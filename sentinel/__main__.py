"""Sentinel command-line entry point."""

from __future__ import annotations

import os
import platform
import sqlite3
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import tomli_w
import typer
from rich.console import Console

from sentinel.config.settings import load_mandate, load_settings
from sentinel.risk.audit import append_audit, read_audit
from sentinel.store.db import connect, default_db_path, run_migrations

RESEARCH_DISCLAIMER = (
    "Research use only; not financial advice. Live trading gives software authority "
    "over real money in your own account; losses are yours. Use official rails only "
    "and start tiny."
)

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Sentinel trading system",
    epilog=RESEARCH_DISCLAIMER,
)
console = Console()

AssetStage = Literal["crypto", "equity", "options"]
_FLAG_BY_STAGE: dict[AssetStage, str] = {
    "crypto": "crypto_stage_enabled",
    "equity": "equity_stage_enabled",
    "options": "options_stage_enabled",
}
_CONFIRM_BY_STAGE: dict[AssetStage, str] = {
    "crypto": "I UNDERSTAND LIVE CRYPTO",
    "equity": "I UNDERSTAND LIVE EQUITY",
    "options": "I UNDERSTAND LIVE OPTIONS",
}
_SECRET_WORDS = ("key", "secret", "token", "password", "signature", "credential")


def _build_real_tui_app():
    """Create the primary TUI wired to the real command service and event bus."""

    from sentinel.core.bus import EventBus
    from sentinel.data.router import DataRouter
    from sentinel.llm.gateway import LLMGateway
    from sentinel.orchestrator.service import build_command_service
    from sentinel.tui.app import SentinelApp

    root = Path.cwd()
    settings = load_settings(root)
    mandate = load_mandate(root)
    conn = connect(default_db_path())
    bus = EventBus()
    router = DataRouter(settings=settings)
    llm = LLMGateway(settings=settings)
    service = build_command_service(
        bus,
        conn,
        settings,
        mandate,
        llm=llm,
        router=router,
    )
    return SentinelApp(event_bus=service.get_event_bus(), command_service=service)


@app.callback()
def _default(ctx: typer.Context) -> None:
    """Launch the TUI when no subcommand is supplied."""

    if ctx.invoked_subcommand is None:
        _build_real_tui_app().run()
        raise typer.Exit(0)


@app.command()
def doctor(
    root: Annotated[
        Path, typer.Option(help="Project root containing config.toml and mandate.toml")
    ] = Path("."),
    live: Annotated[bool, typer.Option("--live", help="Include live rail readiness checks")] = False,
) -> None:
    """Print a redacted Phase 0 readiness report."""

    project_root = root.resolve()
    settings = load_settings(project_root)
    mandate = load_mandate(project_root)
    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    console.print("[bold]Sentinel doctor[/bold]")
    console.print(f"Python: {platform.python_version()} ({platform.machine()})")
    for key in (
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ALPHA_VANTAGE_KEY",
        "FINNHUB_KEY",
    ):
        console.print(f"{key}: {'present' if bool(os.environ.get(key)) else 'absent'}")
    console.print(f"DB writable: yes ({default_db_path()})")
    console.print(
        f"config.toml valid: yes (provider={settings.llm.provider}, "
        f"quick={settings.llm.quick_model}, deep={settings.llm.deep_model})"
    )
    console.print(f"mandate.toml valid: yes ({len(mandate.symbol_universe)} symbols)")
    _print_options_doctor(settings, mandate)
    _print_kill_switch_doctor()
    if live:
        _print_live_doctor(settings, mandate)


def _print_options_doctor(settings: Any, mandate: Any) -> None:
    console.print("[bold]Options readiness[/bold]")
    console.print(f"options enabled: {'yes' if settings.options.enabled or mandate.options.enabled else 'no'}")
    _print_options_tables_check()
    _print_option_chain_check(settings, mandate)
    _print_risk_free_check(settings)


def _print_options_tables_check() -> None:
    expected = {"option_positions", "iv_history", "live_orders", "reconciliations"}
    try:
        conn = connect(default_db_path())
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('option_positions', 'iv_history', 'live_orders', 'reconciliations')"
            ).fetchall()
        finally:
            conn.close()
        found = {str(row[0]) for row in rows}
        missing = sorted(expected - found)
    except Exception as exc:
        console.print(f"options tables migrated: error ({exc})")
        return
    if missing:
        console.print(f"options tables migrated: no (missing {', '.join(missing)})")
    else:
        console.print("options tables migrated: yes")


def _print_option_chain_check(settings: Any, mandate: Any) -> None:
    if os.environ.get("SENTINEL_DOCTOR_NETWORK") != "1":
        console.print("option chain fetch: skipped (offline; set SENTINEL_DOCTOR_NETWORK=1)")
        return
    symbol = next(iter(getattr(mandate.options, "underlying_universe", []) or ["SPY"]), "SPY")
    try:
        from datetime import UTC

        import yfinance as yf

        from sentinel.data.loaders.yfinance_options import YFinanceOptionsLoader

        ticker = yf.Ticker(symbol)
        history = ticker.history(period="5d", interval="1d", auto_adjust=False, timeout=8)
        close = history.get("Close") if history is not None else None
        spot = float(close.dropna().iloc[-1]) if close is not None and not close.dropna().empty else 0.0
        chain = YFinanceOptionsLoader().get_option_chain(
            symbol,
            datetime.now(UTC),
            spot,
            float(settings.options.default_risk_free_rate),
            0.0,
            min_dte=21,
            max_dte=60,
            run_id="doctor",
        )
    except Exception as exc:
        console.print(f"option chain fetch: unavailable ({exc})")
        return
    if chain is None:
        console.print(f"option chain fetch: unavailable ({symbol}; provider returned no chain)")
    else:
        console.print(
            f"option chain fetch: ok ({symbol}; {len(chain.expiries)} expiries, {len(chain.quotes)} quotes)"
        )


def _print_risk_free_check(settings: Any) -> None:
    if os.environ.get("SENTINEL_DOCTOR_NETWORK") != "1":
        console.print("^IRX risk-free rate: skipped (offline; set SENTINEL_DOCTOR_NETWORK=1)")
        return
    try:
        from sentinel.data.loaders.rates import risk_free_rate

        result = risk_free_rate(datetime.now(), settings=settings)
    except Exception as exc:
        console.print(f"^IRX risk-free rate: unavailable ({exc})")
        return
    note = f"; {result.note}" if result.note else ""
    console.print(f"^IRX risk-free rate: ok ({result.provider}, {result.value:.4%}{note})")


def _print_kill_switch_doctor() -> None:
    from sentinel.risk.killswitch import kill_path

    path = kill_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writable = os.access(path.parent, os.W_OK)
    except Exception as exc:
        console.print(f"kill-switch path writable: no ({path}; {exc})")
        return
    console.print(f"kill-switch path writable: {'yes' if writable else 'no'} ({path})")


def _print_live_doctor(settings: Any, mandate: Any) -> None:
    console.print("[bold]Live readiness[/bold]")
    rails: list[tuple[str, bool]] = [
        ("crypto", bool(mandate.live.crypto_stage_enabled or settings.execution.robinhood.enabled)),
        ("equity", bool(mandate.live.equity_stage_enabled or settings.execution.robinhood_agentic.enabled)),
        ("options", bool(mandate.live.options_stage_enabled or settings.execution.robinhood_agentic.enabled)),
    ]
    for rail, enabled in rails:
        if not enabled:
            continue
        if rail == "crypto":
            api_key = os.environ.get(settings.execution.robinhood.api_key_env)
            private_key = os.environ.get(settings.execution.robinhood.private_key_env)
            console.print(f"{rail}: enabled")
            console.print(f"  api key: {'present' if api_key else 'absent'}")
            console.print(f"  private key: {'present' if private_key else 'absent'}")
            console.print(f"  signing probe: {'ready' if api_key and private_key else 'blocked'}")
        else:
            mcp = os.environ.get(settings.execution.robinhood_agentic.mcp_endpoint_env)
            console.print(f"{rail}: enabled")
            console.print(f"  MCP credentials: {'present' if mcp else 'absent'}")
            console.print(f"  account linked: {'ready' if mcp else 'blocked'}")
        console.print(
            "  funding cap: "
            f"configured allocation ${mandate.live.max_account_allocation_usd:,.2f}"
        )


@app.command()
def tui() -> None:
    """Launch the Textual shell."""

    _build_real_tui_app().run()


@app.command()
def reconcile(
    adopt_broker: Annotated[
        bool,
        typer.Option("--adopt-broker", help="Adopt broker positions/cash into Sentinel's mirror"),
    ] = False,
) -> None:
    """Run one live broker reconciliation pass and print the redacted diff."""

    import asyncio

    from sentinel.orchestrator.service import build_command_service

    async def _run() -> None:
        service = build_command_service()
        result = await service.run_reconciliation_once(adopt_broker=adopt_broker)
        console.print("[bold]Sentinel reconciliation[/bold]")
        console.print(f"ok: {getattr(result, 'ok', True)}")
        console.print_json(data=_redact(getattr(result, "diff", lambda: {})()))

    asyncio.run(_run())


def _phase0_stub(name: str) -> None:
    console.print(f"sentinel {name} is not implemented in Phase 0; interface reserved.")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if any(word in str(key).lower() for word in _SECRET_WORDS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@app.command()
def portfolio(
    live: Annotated[bool, typer.Option("--live", help="Fetch live positions from Robinhood MCP")] = False,
) -> None:
    """Print portfolio — paper positions from SQLite, or live from Robinhood."""

    if not live:
        _print_paper_portfolio()
        return

    import asyncio

    asyncio.run(_print_live_portfolio())


def _print_paper_portfolio() -> None:
    from sentinel.store.db import connect, default_db_path, run_migrations

    conn = connect(default_db_path())
    run_migrations(conn)
    rows = conn.execute(
        "SELECT symbol, qty, avg_cost, stop_pct, tp_pct FROM positions ORDER BY symbol"
    ).fetchall()
    console.print("[bold]Sentinel paper portfolio[/bold]")
    if not rows:
        console.print("No open positions.")
        return
    console.print(f"{'SYMBOL':<8} {'QTY':>10} {'AVG COST':>12} {'STOP %':>8} {'TP %':>8}")
    console.print("─" * 52)
    for row in rows:
        stop = f"{row['stop_pct']:.1f}" if row["stop_pct"] is not None else "—"
        tp = f"{row['tp_pct']:.1f}" if row["tp_pct"] is not None else "—"
        console.print(
            f"{row['symbol']:<8} {float(row['qty'] or 0):>10.4f} "
            f"${float(row['avg_cost'] or 0):>11,.2f} {stop:>8} {tp:>8}"
        )


async def _print_live_portfolio() -> None:
    import os

    from sentinel.config.settings import load_settings
    from sentinel.execution.rh_viewer import (
        build_session,
        dec,
        fetch_accounts,
        fetch_equity_positions,
        fetch_option_positions,
        fetch_portfolio,
        mask,
    )

    settings = load_settings(Path.cwd())
    session = build_session(settings.execution.robinhood_agentic)
    if session._token is None:
        console.print(
            f"[red]RH_AGENTIC_MCP_TOKEN not set.[/red]\n"
            f"Add it to your .env: {settings.execution.robinhood_agentic.mcp_token_env}=<token>\n"
            "Get your token at robinhood.com/us/en/agentic-trading/"
        )
        raise typer.Exit(1)

    console.print("[bold]Live Robinhood portfolio[/bold]")
    accounts = await fetch_accounts(session)

    for account in accounts:
        acct_num = str(account.get("account_number") or "")
        nickname = account.get("nickname") or account.get("brokerage_account_type") or ""
        agentic_flag = "  ← agent trades here" if account.get("agentic_allowed") else ""
        console.print(f"\n[bold]{mask(acct_num)}[/bold]  {nickname}{agentic_flag}")

        port = await fetch_portfolio(session, acct_num)
        total = dec(port.get("total_value"))
        equity = dec(port.get("equity_value"))
        cash = dec(port.get("cash"))
        crypto = dec(port.get("crypto_value"))
        console.print(f"  Portfolio  ${total:>12,.2f}")
        console.print(f"  Equity     ${equity:>12,.2f}")
        console.print(f"  Cash       ${cash:>12,.2f}")
        if crypto > 0:
            console.print(f"  Crypto     ${crypto:>12,.2f}")

        positions = await fetch_equity_positions(session, acct_num)
        if positions:
            console.print(f"\n  {'SYMBOL':<8} {'QTY':>10} {'AVG COST':>12}")
            console.print(f"  {'─'*8} {'─'*10} {'─'*12}")
            for pos in positions:
                sym = str(pos.get("symbol") or "")
                qty = dec(pos.get("quantity"))
                avg = dec(pos.get("average_buy_price"))
                console.print(f"  {sym:<8} {float(qty):>10.4f} ${float(avg):>11,.2f}")

        options = await fetch_option_positions(session, acct_num)
        if options:
            console.print(f"\n  Options ({len(options)} position(s))")
            for opt in options:
                sym = str(opt.get("chain_symbol") or opt.get("symbol") or "")
                qty = dec(opt.get("quantity"))
                avg = dec(opt.get("average_price"))
                expiry = str(opt.get("expiration_date") or "—")[:10]
                opt_type = str(opt.get("type") or "")
                console.print(f"  {sym:<8} {opt_type:<6} qty={float(qty):g}  avg=${float(avg):,.2f}  exp={expiry}")


@app.command()
def run(
    symbol: Annotated[str, typer.Argument(help="Symbol to run through Sentinel")],
    date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD as-of date")] = None,
    depth: Annotated[str, typer.Option("--depth", help="fast|standard|deep")] = "standard",
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    import asyncio
    from datetime import UTC, datetime

    from sentinel.orchestrator.runner import OrchestratorRunner

    async def _run() -> None:
        as_of = (
            datetime.fromisoformat(date).replace(tzinfo=UTC)
            if date is not None
            else datetime.now(UTC)
        )
        runner = OrchestratorRunner()
        state = await runner.run(symbol, as_of=as_of, depth=depth)
        if json_output:
            console.print(state.model_dump_json(indent=2))
            return
        console.print(f"[bold]Sentinel run {state.run_id}[/bold] {state.symbol}")
        console.print(f"status: {state.status}")
        console.print(f"action: {state.trade_proposal.action if state.trade_proposal else 'n/a'}")
        console.print(f"verdict: {state.pm_decision.verdict if state.pm_decision else 'n/a'}")
        console.print(f"order: {state.final_order.model_dump_json() if state.final_order else 'none'}")
        if state.error:
            console.print(f"[yellow]note: {state.error}[/yellow]")

    asyncio.run(_run())


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run id to resume")],
    depth: Annotated[str, typer.Option("--depth", help="fast|standard|deep")] = "standard",
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    import asyncio

    from sentinel.orchestrator.runner import OrchestratorRunner

    async def _resume() -> None:
        runner = OrchestratorRunner()
        state = await runner.resume(run_id, depth=depth)
        if json_output:
            console.print(state.model_dump_json(indent=2))
            return
        console.print(f"[bold]Sentinel resumed {state.run_id}[/bold] {state.symbol}")
        console.print(f"status: {state.status}")
        console.print(f"action: {state.trade_proposal.action if state.trade_proposal else 'n/a'}")
        console.print(f"verdict: {state.pm_decision.verdict if state.pm_decision else 'n/a'}")
        console.print(f"order: {state.final_order.model_dump_json() if state.final_order else 'none'}")
        if state.error:
            console.print(f"[yellow]note: {state.error}[/yellow]")

    asyncio.run(_resume())


@app.command()
def backtest(
    mode: Annotated[str, typer.Option("--mode", help="Backtest mode: rule or agent")] = "rule",
    symbol: Annotated[str, typer.Option("--symbol", help="Symbol to replay")] = "SPY",
    strategy: Annotated[str, typer.Option("--strategy", help="Rule strategy name")] = "sma_cross",
    start: Annotated[str | None, typer.Option("--start", help="YYYY-MM-DD start date")] = None,
    end: Annotated[str | None, typer.Option("--end", help="YYYY-MM-DD end date")] = None,
    csv: Annotated[Path | None, typer.Option("--csv", help="Offline OHLCV CSV path")] = None,
    cadence: Annotated[str, typer.Option("--cadence", help="Agent cadence")] = "weekly",
    short_window: Annotated[int, typer.Option("--short-window", help="SMA short window")] = 20,
    long_window: Annotated[int, typer.Option("--long-window", help="SMA long window")] = 50,
    starting_cash: Annotated[
        float | None, typer.Option("--starting-cash", help="Starting cash override")
    ] = None,
    confirm: Annotated[
        str | None, typer.Option("--confirm", help='Agent mode requires "RUN AGENT BACKTEST"')
    ] = None,
) -> None:
    from datetime import date

    from sentinel.backtest.engine import BacktestConfig, run_backtest

    normalized_mode = mode.lower()
    if normalized_mode == "agent":
        if confirm != "RUN AGENT BACKTEST":
            console.print('[red]agent mode requires --confirm "RUN AGENT BACKTEST"[/red]')
            raise typer.Exit(1)
        console.print("[yellow]agent replay CLI awaits orchestrator pipeline wiring; use API seam.[/yellow]")
        raise typer.Exit(1)
    strategy_params: dict[str, object] = {}
    if strategy == "sma_cross":
        strategy_params = {"short_window": short_window, "long_window": long_window}
    result = run_backtest(
        BacktestConfig(
            symbol=symbol,
            mode="rule",
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            strategy=strategy,
            strategy_params=strategy_params,
            csv_path=csv,
            cadence=cadence,
            starting_cash=starting_cash,
        )
    )
    console.print(f"[bold]Backtest {symbol}[/bold] strategy={strategy}")
    console.print(f"total_return: {result.total_return:.4f}")
    console.print(f"sharpe: {result.sharpe:.4f}")
    console.print(f"max_drawdown: {result.max_drawdown:.4f}")


eval_app = typer.Typer(help="Shared evaluation harness: compare pipelines on identical data.")
app.add_typer(eval_app, name="eval")


@eval_app.command("pipelines")
def eval_pipelines() -> None:
    """List the pipelines the harness can compare."""

    from sentinel.eval.pipelines import PIPELINES

    for spec in PIPELINES.values():
        console.print(f"[bold]{spec.name}[/bold] ({spec.kind}): {spec.description}")


@eval_app.command("run")
def eval_run(
    pipeline: Annotated[
        list[str], typer.Option("--pipeline", "-p", help="Pipeline name (repeatable); see `sentinel eval pipelines`")
    ],
    symbol: Annotated[list[str], typer.Option("--symbol", "-s", help="Symbol (repeatable)")],
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD window start")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD window end")],
    experiment: Annotated[
        str | None, typer.Option("--experiment", help="Experiment id (default: derived from args)")
    ] = None,
    cadence: Annotated[str, typer.Option("--cadence", help="Agent decision cadence: daily|weekly|monthly|N")] = "weekly",
    csv: Annotated[
        list[str] | None,
        typer.Option("--csv", help="Offline OHLCV as SYMBOL=path.csv (repeatable); default: data router"),
    ] = None,
    short_window: Annotated[int, typer.Option("--short-window", help="sma_cross short window")] = 20,
    long_window: Annotated[int, typer.Option("--long-window", help="sma_cross long window")] = 50,
    starting_cash: Annotated[float | None, typer.Option("--starting-cash")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Where experiment.db + results.csv go")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print rows as JSON instead of a table")] = False,
) -> None:
    """Run one experiment (fixed symbols + window) for the chosen pipelines and print the metrics table."""

    import json
    from datetime import date

    from sentinel.eval import ExperimentSpec, render_results_table, run_experiment

    csv_paths: dict[str, Path] = {}
    for item in csv or []:
        if "=" not in item:
            console.print("[red]--csv expects SYMBOL=path.csv[/red]")
            raise typer.Exit(1)
        sym, path = item.split("=", 1)
        csv_paths[sym.upper()] = Path(path)
    normalized_cadence: str | int = int(cadence) if cadence.isdigit() else cadence
    experiment_id = experiment or f"{'-'.join(s.upper() for s in symbol)}_{start}_{end}"
    spec = ExperimentSpec(
        experiment_id=experiment_id,
        symbols=[s.upper() for s in symbol],
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        pipelines=list(pipeline),
        cadence=normalized_cadence,
        starting_cash=starting_cash,
        strategy_params={"short_window": short_window, "long_window": long_window},
        csv_paths=csv_paths,
        out_dir=out_dir,
    )
    rows = run_experiment(spec)
    if json_output:
        console.print(json.dumps([row.as_record() for row in rows], indent=2, default=str))
    else:
        render_results_table(rows, console=console)
    console.print(f"results: {spec.csv_path()}  (db: {spec.db_path()})")


@eval_app.command("report")
def eval_report(
    experiment: Annotated[str, typer.Option("--experiment", help="Experiment id to rebuild")],
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Experiment dir (default: ~/.sentinel/experiments/<id>)")] = None,
    csv_out: Annotated[Path | None, typer.Option("--csv-out", help="Also write the table to this CSV")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print rows as JSON")] = False,
) -> None:
    """Rebuild the metrics table for a stored experiment without re-running anything."""

    import json

    from sentinel.eval import load_result_rows, render_results_table, results_to_csv
    from sentinel.store.db import sentinel_home

    root = out_dir or (sentinel_home() / "experiments" / experiment)
    db_path = root / "experiment.db"
    if not db_path.exists():
        console.print(f"[red]no experiment database at {db_path}[/red]")
        raise typer.Exit(1)
    conn = connect(db_path)
    try:
        run_migrations(conn)
        rows = load_result_rows(conn, experiment)
    finally:
        conn.close()
    if not rows:
        console.print(f"[yellow]no stored rows for experiment {experiment!r}[/yellow]")
        raise typer.Exit(1)
    if json_output:
        console.print(json.dumps([row.as_record() for row in rows], indent=2, default=str))
    else:
        render_results_table(rows, console=console)
    if csv_out is not None:
        results_to_csv(rows, csv_out)
        console.print(f"wrote {csv_out}")


@app.command()
def portfolio(
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    """Print the current paper portfolio."""


    from sentinel.execution.portfolio import load_portfolio

    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        portfolio_state = load_portfolio(conn)
        if json_output:
            console.print(portfolio_state.model_dump_json(indent=2))
            return
        console.print("[bold]Paper portfolio[/bold]")
        console.print(f"cash: ${portfolio_state.cash:,.2f}")
        console.print(f"equity: ${portfolio_state.equity:,.2f}")
        for position in portfolio_state.positions:
            console.print(
                f"{position.symbol} qty={position.qty:g} avg=${position.avg_cost:,.2f} "
                f"stop={position.stop_loss_pct} tp={position.take_profit_pct}"
            )
        if not portfolio_state.positions:
            console.print("no open positions")
    finally:
        conn.close()


@app.command()
def history(
    symbol: Annotated[str | None, typer.Option("--symbol", help="Filter by symbol")] = None,
    debate: Annotated[
        str | None, typer.Option("--debate", help="Filter by debate switch: on|off")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    """Print recent decision history."""

    import json

    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        clauses: list[str] = []
        params: list[object] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if debate is not None:
            if debate.lower() not in {"on", "off"}:
                console.print("[red]--debate must be 'on' or 'off'[/red]")
                raise typer.Exit(1)
            clauses.append("debate_enabled = ?")
            params.append(1 if debate.lower() == "on" else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM runs {where} ORDER BY COALESCE(finished_at, created_at, as_of) DESC LIMIT 50",
            params,
        ).fetchall()
        records = [dict(row) for row in rows]
        if json_output:
            console.print(json.dumps(records, indent=2, default=str))
            return
        console.print("[bold]Decision history[/bold]")
        for row in records:
            debate_flag = row.get("debate_enabled")
            debate = "—" if debate_flag is None else ("debate=on" if debate_flag else "debate=off")
            console.print(
                f"{str(row.get('as_of') or '')[:10]} {row.get('run_id')} "
                f"{row.get('symbol') or '—'} {row.get('action') or '—'} "
                f"{row.get('verdict') or row.get('status') or '—'} {debate}"
            )
        if not records:
            console.print("no runs")
    finally:
        conn.close()


@app.command()
def reflect() -> None:
    from sentinel.memory.reflection_job import run_reflection_job

    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        lessons = run_reflection_job(conn)
    finally:
        conn.close()
    console.print(f"reflected {len(lessons)} journal entr{'y' if len(lessons) == 1 else 'ies'}")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def memory(ctx: typer.Context) -> None:
    import json

    from sentinel.memory.recall import (
        forget_lesson,
        get_lesson,
        list_lessons,
        search_lessons,
    )

    args = list(ctx.args)
    json_output = "--json" in args
    args = [arg for arg in args if arg != "--json"]
    command = args[0] if args else "list"
    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        if command == "list":
            lessons = list_lessons(conn)
            if json_output:
                console.print(json.dumps([lesson.model_dump(mode="json") for lesson in lessons], indent=2))
                return
            for lesson in lessons:
                console.print(f"{lesson.lesson_id} [{lesson.grade}] {lesson.symbol}: {lesson.lesson}")
        elif command == "show" and len(args) >= 2:
            lesson = get_lesson(conn, args[1])
            if lesson is None:
                raise typer.BadParameter(f"unknown lesson_id {args[1]!r}")
            console.print(lesson.model_dump_json(indent=2) if json_output else f"{lesson.lesson_id}: {lesson.lesson}")
        elif command == "search" and len(args) >= 2:
            lessons = search_lessons(conn, " ".join(args[1:]))
            if json_output:
                console.print(json.dumps([lesson.model_dump(mode="json") for lesson in lessons], indent=2))
                return
            for lesson in lessons:
                console.print(f"{lesson.lesson_id} [{lesson.grade}] {lesson.symbol}: {lesson.lesson}")
        elif command == "forget" and len(args) >= 2:
            removed = forget_lesson(conn, args[1])
            if json_output:
                console.print(json.dumps({"forgotten": removed}))
                return
            console.print("forgotten" if removed else "not found")
        else:
            raise typer.BadParameter("expected memory list|show <id>|search <query>|forget <id>")
    finally:
        conn.close()


@app.command()
def graduate(
    stage: Annotated[str, typer.Argument(help="crypto|equity|options")],
    root: Annotated[
        Path, typer.Option(help="Project root containing mandate.toml and config.toml")
    ] = Path("."),
) -> None:
    """Enable one live stage after deterministic prerequisite checks."""

    from sentinel.risk.killswitch import is_engaged

    asset = _normalize_stage(stage)
    if is_engaged():
        console.print("[red]refusing graduation: kill switch is engaged[/red]")
        raise typer.Exit(1)

    project_root = root.resolve()
    settings = load_settings(project_root)
    mandate = load_mandate(project_root)
    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        blockers = _graduation_blockers(asset, conn, settings, mandate)
    finally:
        conn.close()
    if blockers:
        console.print("[red]graduation refused[/red]")
        for blocker in blockers:
            console.print(f"- {blocker}")
        raise typer.Exit(1)

    flag = _FLAG_BY_STAGE[asset]
    console.print(f"Will change mandate.live.{flag}: false -> true")
    phrase = _CONFIRM_BY_STAGE[asset]
    typed = typer.prompt(f'Type "{phrase}" to proceed')
    if typed != phrase:
        console.print("[red]confirmation did not match; no change made[/red]")
        raise typer.Exit(1)
    _write_stage_flag(project_root / "mandate.toml", flag, True)
    append_audit("live_stage_changed", {"stage": asset, "flag": flag, "enabled": True}, actor="cli")
    console.print(f"graduated {asset}: enabled")


@app.command()
def demote(
    stage: Annotated[str, typer.Argument(help="crypto|equity|options")],
    root: Annotated[
        Path, typer.Option(help="Project root containing mandate.toml")
    ] = Path("."),
) -> None:
    """Disable one live stage immediately, with no confirmation."""

    asset = _normalize_stage(stage)
    flag = _FLAG_BY_STAGE[asset]
    _write_stage_flag(root.resolve() / "mandate.toml", flag, False)
    append_audit("live_stage_changed", {"stage": asset, "flag": flag, "enabled": False}, actor="cli")
    console.print(f"demoted {asset}: disabled")


def _normalize_stage(value: str) -> AssetStage:
    normalized = value.lower()
    if normalized == "option":
        normalized = "options"
    if normalized not in _FLAG_BY_STAGE:
        raise typer.BadParameter("expected crypto, equity, or options")
    return cast(AssetStage, normalized)


def _graduation_blockers(
    stage: AssetStage,
    conn: sqlite3.Connection,
    settings: Any,
    mandate: Any,
) -> list[str]:
    blockers: list[str] = []
    if mandate.live.max_live_order_notional_usd <= 0 or mandate.live.max_account_allocation_usd <= 0:
        blockers.append("live mandate section is not filled with positive live limits")
    if stage == "crypto":
        if _count_crypto_paper_runs(conn) < 30:
            blockers.append("crypto requires at least 30 completed paper runs")
        if not _pnl_history_present(conn):
            blockers.append("crypto paper P&L history is absent")
        if not os.environ.get(settings.execution.robinhood.api_key_env):
            blockers.append(f"{settings.execution.robinhood.api_key_env} is absent")
        if not os.environ.get(settings.execution.robinhood.private_key_env):
            blockers.append(f"{settings.execution.robinhood.private_key_env} is absent")
    elif stage == "equity":
        if _count_equity_paper_days(conn) < 60:
            blockers.append("equity requires at least 60 paper equity track-record days")
        if not os.environ.get(settings.execution.robinhood_agentic.mcp_endpoint_env):
            blockers.append(f"{settings.execution.robinhood_agentic.mcp_endpoint_env} is absent")
    else:
        if not bool(mandate.live.equity_stage_enabled):
            blockers.append("options require live equity stage to be active first")
        if _equity_stage_age_days() < 30:
            blockers.append("options require live equity stage active for at least 30 days")
        if _count_closed_option_positions(conn) < 50:
            blockers.append("options require at least 50 paper option positions closed")
        if not os.environ.get(settings.execution.robinhood_agentic.mcp_endpoint_env):
            blockers.append(f"{settings.execution.robinhood_agentic.mcp_endpoint_env} is absent")
    return blockers


def _write_stage_flag(path: Path, flag: str, enabled: bool) -> None:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    root = data.get("mandate", data)
    if not isinstance(root, dict):
        raise typer.BadParameter("mandate.toml must contain a mandate table")
    live = root.setdefault("live", {})
    if not isinstance(live, dict):
        raise typer.BadParameter("mandate.live must be a table")
    live[flag] = enabled
    with path.open("wb") as handle:
        tomli_w.dump(data, handle)


def _count_crypto_paper_runs(conn: sqlite3.Connection) -> int:
    return _query_count(
        conn,
        """
        SELECT COUNT(*) FROM runs
        WHERE UPPER(symbol) IN ('BTC-USD','ETH-USD','BTC','ETH')
          AND COALESCE(status, '') IN ('completed', 'filled', 'success')
          AND (mode IS NULL OR LOWER(mode) LIKE '%paper%' OR LOWER(mode) = 'decision')
        """,
    )


def _count_equity_paper_days(conn: sqlite3.Connection) -> int:
    return _query_count(
        conn,
        """
        SELECT COUNT(DISTINCT substr(COALESCE(as_of, created_at), 1, 10)) FROM runs
        WHERE UPPER(symbol) NOT IN ('BTC-USD','ETH-USD','BTC','ETH')
          AND COALESCE(status, '') IN ('completed', 'filled', 'success')
          AND (mode IS NULL OR LOWER(mode) LIKE '%paper%' OR LOWER(mode) = 'decision')
        """,
    )


def _pnl_history_present(conn: sqlite3.Connection) -> bool:
    return _query_count(conn, "SELECT COUNT(*) FROM equity_curve") > 0 or _query_count(
        conn,
        "SELECT COUNT(*) FROM journal WHERE realized_ret IS NOT NULL",
    ) > 0


def _count_closed_option_positions(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "option_positions"):
        return 0
    columns = _table_columns(conn, "option_positions")
    if "closed_at" in columns:
        return _query_count(conn, "SELECT COUNT(*) FROM option_positions WHERE closed_at IS NOT NULL")
    if "status" in columns:
        return _query_count(conn, "SELECT COUNT(*) FROM option_positions WHERE status = 'closed'")
    return 0


def _equity_stage_age_days() -> int:
    for row in read_audit():
        if row["kind"] != "live_stage_changed" or not isinstance(row["payload"], dict):
            continue
        payload = row["payload"]
        if (
            payload.get("stage") == "equity"
            and payload.get("enabled") is True
            and isinstance(row["ts"], str)
        ):
            try:
                ts = datetime_from_iso(row["ts"])
            except ValueError:
                continue
            from datetime import UTC, datetime

            return (datetime.now(UTC) - ts).days
    return 0


def datetime_from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _query_count(conn: sqlite3.Connection, sql: str) -> int:
    try:
        value = conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return 0
    return int(value or 0)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _verify_and_report() -> None:
    import asyncio
    from sentinel.execution.mcp_session import RhAgenticMcpSession

    console.print("\nVerifying connection to Robinhood MCP...")
    try:
        async def _verify() -> int:
            session = RhAgenticMcpSession()
            result = await session.list_tools()
            tools = result.get("tools", [])
            return len(tools) if isinstance(tools, list) else 0

        n = asyncio.run(_verify())
        console.print(f"[green]Connected![/green]  {n} MCP tools available.")
        console.print("You can now run: sentinel portfolio --live")
    except Exception as exc:
        console.print(f"[yellow]Token saved but MCP verify failed: {exc}[/yellow]")


@app.command()
def auth(
    revoke: Annotated[bool, typer.Option("--revoke", help="Remove stored tokens")] = False,
    status: Annotated[bool, typer.Option("--status", help="Check auth status only")] = False,
    code: Annotated[str | None, typer.Option("--code", help="Manually paste the ?code= from the redirect URL")] = None,
) -> None:
    """Authenticate with Robinhood via OAuth (opens browser once, stores tokens).

    If the browser redirect fails, copy the ?code= value from the URL bar and run:
      sentinel auth --code <value>
    """

    import asyncio
    import time
    from sentinel.execution.rh_auth import (
        _TOKEN_FILE,
        clear_tokens,
        exchange_saved_code,
        get_access_token,
        load_tokens,
        run_oauth_flow,
    )

    if revoke:
        clear_tokens()
        console.print("Robinhood tokens revoked.")
        return

    if status:
        token = get_access_token()
        if token:
            tokens = load_tokens()
            expires_in = int(tokens.get("expires_at", 0) - time.time())
            console.print(f"[green]Authenticated[/green]  token expires in {expires_in}s  ({_TOKEN_FILE})")
        else:
            console.print("[red]Not authenticated.[/red]  Run: sentinel auth")
        return

    if code:
        console.print("Exchanging authorization code for tokens...")
        try:
            asyncio.run(exchange_saved_code(code))
        except Exception as exc:
            console.print(f"[red]Token exchange failed: {exc}[/red]")
            raise typer.Exit(1)
        _verify_and_report()
        return

    # Already authenticated?
    existing = get_access_token()
    if existing:
        tokens = load_tokens()
        expires_in = int(tokens.get("expires_at", 0) - time.time())
        console.print(f"[green]Already authenticated[/green]  (token valid for {expires_in}s)")
        console.print("Use --revoke to force re-authentication.")
        return

    console.print("[bold]Robinhood OAuth authentication[/bold]")
    console.print("This will open your browser. Log in and authorize Sentinel.")
    console.print(RESEARCH_DISCLAIMER)
    console.print()

    try:
        asyncio.run(run_oauth_flow())
        _verify_and_report()
    except TimeoutError:
        console.print(
            "\n[yellow]Timed out waiting for browser callback.[/yellow]\n"
            "Visit the URL above in your browser, log in to Robinhood, then:\n"
            "  1. After approving, your browser will redirect to localhost:54321\n"
            "  2. If it shows 'connection refused', look at the URL bar — it contains ?code=XXXX\n"
            "  3. Copy that code value and run:\n"
            "       sentinel auth --code XXXX"
        )
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def kill(
    state: Annotated[str, typer.Argument(help="on|off")],
    flatten: Annotated[bool, typer.Option("--flatten", help="Also market-close live positions")] = False,
) -> None:
    from sentinel.risk.killswitch import FLATTEN_CONFIRMATION, disengage, engage, is_engaged

    normalized = state.lower()
    if normalized == "on":
        confirmation = None
        if flatten:
            confirmation = typer.prompt(f'Type "{FLATTEN_CONFIRMATION}" to flatten live positions')
        settings = load_settings(Path.cwd())
        mandate = load_mandate(Path.cwd())
        engage(
            actor="cli",
            brokers=_live_brokers_from_settings(settings, mandate),
            flatten=flatten,
            flatten_confirmation=confirmation,
        )
    elif normalized == "off":
        disengage(actor="cli")
    else:
        raise typer.BadParameter("expected 'on' or 'off'")
    console.print(f"kill switch: {'on' if is_engaged() else 'off'}")


def _live_brokers_from_settings(settings: Any, mandate: Any) -> dict[str, Any]:
    brokers: dict[str, Any] = {}
    if bool(settings.execution.robinhood.enabled) and bool(mandate.live.crypto_stage_enabled):
        from sentinel.execution.robinhood_crypto import RobinhoodCryptoBroker

        brokers["robinhood_crypto"] = RobinhoodCryptoBroker(settings.execution.robinhood)
    if bool(settings.execution.robinhood_agentic.enabled) and bool(
        mandate.live.equity_stage_enabled or mandate.live.options_stage_enabled
    ):
        from sentinel.execution.robinhood_agentic import RobinhoodAgenticBroker

        brokers["robinhood_agentic"] = RobinhoodAgenticBroker(settings.execution.robinhood_agentic)
    return brokers


@app.command()
def snapshot(symbol: Annotated[str, typer.Argument(help="Symbol to snapshot")]) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from sentinel.config.settings import load_settings
    from sentinel.core.ids import new_run_id
    from sentinel.data.loaders import LocalDataLoader
    from sentinel.data.router import DataRouter
    from sentinel.data.snapshot import build_snapshot, snapshot_sidecar_paths

    settings = load_settings(Path.cwd())
    as_of = datetime.now(UTC)
    run_id = new_run_id()
    router = DataRouter(settings=settings)
    try:
        data_snapshot = build_snapshot(router, symbol, as_of, run_id, settings)
    except Exception as exc:
        console.print(f"[yellow]real provider snapshot failed, using local fallback: {exc}[/yellow]")
        router = DataRouter(loaders=[LocalDataLoader()], settings=settings)
        data_snapshot = build_snapshot(router, symbol, as_of, run_id, settings)

    sidecars = snapshot_sidecar_paths(data_snapshot)
    console.print(f"run_id: {data_snapshot.run_id}")
    console.print(f"ohlcv: {data_snapshot.ohlcv_path}")
    console.print(f"indicators: {data_snapshot.indicators_path}")
    console.print(f"news: {sidecars['news']}")
    console.print(f"fundamentals: {sidecars['fundamentals']}")
    console.print(f"providers: {data_snapshot.providers_used}")


@app.command()
def export(
    run_id: Annotated[str, typer.Argument(help="Run id or backtest id to export")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Output path")] = None,
    csv_output: Annotated[bool, typer.Option("--csv", help="Export CSV files instead of markdown")] = False,
    kind: Annotated[str, typer.Option("--kind", help="auto, run, or backtest")] = "auto",
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    """Export a run bundle to markdown or CSV."""

    from sentinel.store.export import CsvExportMode, export_csv_bundle, export_run_bundle

    if kind not in {"auto", "run", "backtest"}:
        raise typer.BadParameter("kind must be one of: auto, run, backtest")
    csv_kind = cast(CsvExportMode, kind)

    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        if csv_output:
            result = export_csv_bundle(conn, run_id, output_dir=output, kind=csv_kind)
        else:
            path = export_run_bundle(conn, run_id, output_path=output)
    finally:
        conn.close()
    if csv_output:
        if json_output:
            console.print_json(
                data={
                    "id": result.export_id,
                    "kind": result.kind,
                    "paths": [str(path) for path in result.paths],
                    "row_counts": result.row_counts,
                }
            )
        else:
            console.print(f"exported {result.kind} {result.export_id} -> {result.paths[0].parent}")
    elif json_output:
        console.print_json(data={"run_id": run_id, "path": str(path)})
    else:
        console.print(f"exported {run_id} -> {path}")


def main() -> None:
    """Console-script hook."""

    app()


if __name__ == "__main__":
    main()

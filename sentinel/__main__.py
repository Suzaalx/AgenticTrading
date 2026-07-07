"""Sentinel command-line entry point."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console

from sentinel.config.settings import load_mandate, load_settings
from sentinel.store.db import connect, default_db_path, run_migrations

app = typer.Typer(invoke_without_command=True, no_args_is_help=False, help="Sentinel trading system")
console = Console()


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
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ALPHA_VANTAGE_KEY", "FINNHUB_KEY"):
        console.print(f"{key}: {'present' if bool(os.environ.get(key)) else 'absent'}")
    console.print(f"DB writable: yes ({default_db_path()})")
    console.print(f"config.toml valid: yes (provider={settings.llm.provider})")
    console.print(f"mandate.toml valid: yes ({len(mandate.symbol_universe)} symbols)")


@app.command()
def tui() -> None:
    """Launch the Textual shell."""

    _build_real_tui_app().run()


def _phase0_stub(name: str) -> None:
    console.print(f"sentinel {name} is not implemented in Phase 0; interface reserved.")


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
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
) -> None:
    """Print recent decision history."""

    import json

    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        if symbol:
            rows = conn.execute(
                "SELECT * FROM runs WHERE symbol = ? ORDER BY COALESCE(finished_at, created_at, as_of) DESC LIMIT 50",
                (symbol.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY COALESCE(finished_at, created_at, as_of) DESC LIMIT 50"
            ).fetchall()
        records = [dict(row) for row in rows]
        if json_output:
            console.print(json.dumps(records, indent=2, default=str))
            return
        console.print("[bold]Decision history[/bold]")
        for row in records:
            console.print(
                f"{str(row.get('as_of') or '')[:10]} {row.get('run_id')} "
                f"{row.get('symbol') or '—'} {row.get('action') or '—'} "
                f"{row.get('verdict') or row.get('status') or '—'}"
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
def kill(state: Annotated[str, typer.Argument(help="on|off")]) -> None:
    from sentinel.risk.killswitch import disengage, engage, is_engaged

    normalized = state.lower()
    if normalized == "on":
        engage(actor="cli")
    elif normalized == "off":
        disengage(actor="cli")
    else:
        raise typer.BadParameter("expected 'on' or 'off'")
    console.print(f"kill switch: {'on' if is_engaged() else 'off'}")


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

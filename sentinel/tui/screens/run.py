"""Live run monitor tab."""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Markdown, Static

from sentinel.core.events import (
    AgentCompleted,
    AgentStarted,
    CostIncurred,
    DebateTurnAdded,
    DecisionMade,
    Event,
    GateEvaluated,
    KillEngaged,
    OrderFilled,
    OrderSubmitted,
    ReconciliationFailed,
    RunStarted,
    StageChanged,
)
from sentinel.core.models import (
    GateResult,
    OptionsAnalystReport,
    OptionStrategyProposal,
    PMDecision,
)
from sentinel.store.db import sentinel_home
from sentinel.tui.widgets.common import (
    append_limited,
    compact_time,
    fetch_rows,
    money,
    signed_money,
)

StageState = Literal["pending", "running", "done", "failed"]

STAGES: tuple[tuple[str, str], ...] = (
    ("fetch", "Fetch data"),
    ("analysts", "Analysts"),
    ("debate", "Debate"),
    ("research_manager", "Research manager"),
    ("trader", "Trader"),
    ("risk_debate", "Risk debate"),
    ("pm_gate", "PM gate"),
    ("mandate", "Mandate check"),
    ("execute", "Execute"),
)


class RunMonitorScreen(Widget):
    """Flagship event-driven run monitor."""

    def __init__(self) -> None:
        super().__init__(id="run-screen")
        self.run_id: str | None = None
        self.symbol = "—"
        self.status = "idle"
        self.total_cost = Decimal("0")
        self._started_at: datetime | None = None
        self._stage_status: dict[str, StageState] = {key: "pending" for key, _ in STAGES}
        self._stage_cost: defaultdict[str, Decimal] = defaultdict(Decimal)
        self._feed: list[str] = []
        self._research_turns: list[str] = []
        self._risk_turns: list[str] = []
        self._decision: PMDecision | None = None
        self._gate: GateResult | None = None
        self._order_text: str | None = None
        self._fill_text: str | None = None
        self._show_debate = True
        self._options_report: OptionsAnalystReport | None = None
        self._candidates: list[dict[str, Any]] = []
        self._chosen_candidate_id: str | None = None
        self._live_alerts: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="screen-body"):
            with Vertical(id="run-left", classes="panel"):
                yield Static("Pipeline", classes="panel-title")
                yield Static(id="pipeline-rail")
            with Vertical(id="run-right"):
                yield Static(id="run-title", classes="panel")
                yield Markdown(id="report-pane", classes="panel")
                yield Static(id="options-card", classes="panel option-panel")
                yield DataTable(id="option-candidates", classes="panel option-panel")
                yield Static(id="decision-card", classes="panel")
                yield Static(id="live-panel", classes="panel live-panel")
                yield Static(id="debate-transcript", classes="panel debate-panel")
                yield Static(id="risk-debate-transcript", classes="panel debate-panel")
                yield Static(id="live-feed", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#option-candidates", DataTable)
        table.cursor_type = "row"
        table.add_columns("✓", "STRATEGY", "LEGS", "NET PREM", "MAX LOSS", "Δ", "VEGA", "THETA")
        self._hydrate_latest_run()
        self._render_all()

    async def handle_event(self, event: Event) -> None:
        """Apply a core event to the run monitor."""

        if isinstance(event, RunStarted):
            self.run_id = event.run_id
            self.symbol = event.symbol
            self.status = "running"
            self._started_at = event.ts
            self._stage_status = {key: "pending" for key, _ in STAGES}
            self._stage_cost.clear()
            self._feed.clear()
            self._research_turns.clear()
            self._risk_turns.clear()
            self._decision = None
            self._gate = None
            self._order_text = None
            self._fill_text = None
            self._options_report = None
            self._chosen_candidate_id = None
            self._candidates = _load_candidates(event.run_id)
            self._feed_line(f"run {event.run_id} started for {event.symbol}")
        elif isinstance(event, StageChanged):
            key = self._stage_key(event.stage)
            self._stage_status[key] = self._status_from_text(event.status)
            self.status = event.status
            self._feed_line(f"stage {event.stage} → {event.status}")
        elif isinstance(event, AgentStarted):
            self._stage_status["analysts"] = "running"
            self._feed_line(f"{event.agent} started ({event.model})")
        elif isinstance(event, AgentCompleted):
            self._stage_status["analysts"] = "running"
            self._stage_cost["analysts"] += event.report.cost_usd
            self.total_cost += event.report.cost_usd
            self._feed_line(
                f"{event.report.agent} done "
                f"{event.report.input_tokens + event.report.output_tokens} tok "
                f"{money(event.report.cost_usd)}"
            )
            await self._set_report(
                f"# Report: {event.report.agent}\n\n"
                f"Model: `{event.report.model}`  "
                f"Cost: **{money(event.report.cost_usd)}**\n\n"
                f"{event.report.content}"
            )
            if isinstance(event.report, OptionsAnalystReport):
                self._options_report = event.report
                self._candidates = _load_candidates(event.run_id)
            if isinstance(event.report, OptionStrategyProposal):
                self._chosen_candidate_id = event.report.candidate_id
        elif isinstance(event, DebateTurnAdded):
            self._stage_status["risk_debate" if event.debate == "risk" else "debate"] = "running"
            speaker = "BULL" if event.turn.speaker == "bull" else "BEAR"
            prefix = "🟢" if event.turn.speaker == "bull" else "🔴"
            line = f"{prefix} {speaker} r{event.turn.round}: {event.turn.argument}"
            if event.debate == "risk":
                self._risk_turns.append(line)
            else:
                self._research_turns.append(line)
            self._feed_line(f"{event.debate} debate turn from {event.turn.speaker}")
        elif isinstance(event, DecisionMade):
            self._decision = event.decision
            self._stage_status["pm_gate"] = "done" if event.decision.verdict == "APPROVE" else "failed"
            self._feed_line(f"PM decision {event.decision.verdict}")
        elif isinstance(event, GateEvaluated):
            self._gate = event.result
            self._stage_status["mandate"] = "done" if event.result.passed else "failed"
            self._feed_line(f"mandate gate {'passed' if event.result.passed else 'failed'}")
        elif isinstance(event, OrderSubmitted):
            self._order_text = (
                f"Order {event.order.order_id}: {event.order.side.upper()} "
                f"{event.order.qty:g} {event.order.symbol} ({event.order.reason})"
            )
            self._stage_status["execute"] = "running"
            if event.order.venue != "paper":
                self._feed_line(f"LIVE venue {event.order.venue}")
            self._feed_line(self._order_text)
        elif isinstance(event, OrderFilled):
            self._fill_text = (
                f"Fill {event.order_id}: {event.fill.qty:g} @ {money(event.fill.price)} "
                f"slippage {signed_money(event.fill.slippage_usd)} "
                f"commission {money(event.fill.commission_usd)}"
            )
            self._stage_status["execute"] = "done"
            self.status = "completed"
            self._feed_line(self._fill_text)
        elif isinstance(event, CostIncurred):
            stage = "analysts" if event.agent else "fetch"
            self._stage_cost[stage] += event.cost_usd
            self.total_cost += event.cost_usd
        elif isinstance(event, ReconciliationFailed):
            append_limited(
                self._live_alerts,
                f"{compact_time(event.ts)} RECONCILIATION_FAILED {event.venue}: {event.diff}",
            )
        elif isinstance(event, KillEngaged):
            append_limited(
                self._live_alerts,
                f"{compact_time(event.ts)} KILL ENGAGED by {event.actor}: {event.cancel_results}",
            )

        self._render_all()

    def note_run_requested(self, run_id: str, symbol: str) -> None:
        """Render optimistic state after a command starts a run."""

        self.run_id = run_id
        self.symbol = symbol
        self.status = "queued"
        self._feed_line(f"command accepted: run {run_id} for {symbol}")
        self._candidates = _load_candidates(run_id)
        self._render_all()

    def toggle_debate(self) -> None:
        """Show/hide debate panels."""

        self._show_debate = not self._show_debate
        for selector in ("#debate-transcript", "#risk-debate-transcript"):
            widget = self.query_one(selector, Static)
            widget.display = self._show_debate

    def has_active_run(self) -> bool:
        """Return whether the current run is in a terminal state."""

        return self.run_id is not None and self.status not in {
            "completed",
            "failed",
            "cancelled",
            "halted",
        }

    async def _set_report(self, markdown: str) -> None:
        await self.query_one("#report-pane", Markdown).update(markdown)

    def _render_all(self) -> None:
        self._render_title()
        self._render_pipeline()
        self._render_options_card()
        self._render_candidates()
        self._render_debate()
        self._render_decision()
        self._render_live_panel()
        self._render_feed()

    def _render_title(self) -> None:
        run_id = self.run_id or "no active run"
        elapsed = "0:00"
        if self._started_at is not None:
            seconds = max(0, int((datetime.now(self._started_at.tzinfo) - self._started_at).total_seconds()))
            elapsed = f"{seconds // 60}:{seconds % 60:02d}"
        self.query_one("#run-title", Static).update(
            f"Run {run_id}  {self.symbol}  {self.status.upper()}  "
            f"{money(self.total_cost)}  {elapsed}  {self._venue_banner()}"
        )

    def _render_pipeline(self) -> None:
        lines = []
        for key, label in STAGES:
            state = self._stage_status.get(key, "pending")
            glyph = {"done": "✓", "running": "▶", "failed": "✗", "pending": "○"}[state]
            cost = self._stage_cost.get(key, Decimal("0"))
            suffix = f" {money(cost)}" if cost else ""
            lines.append(f"{glyph} {label}{suffix}")
        self.query_one("#pipeline-rail", Static).update("\n".join(lines))

    def _render_debate(self) -> None:
        research = "\n".join(self._research_turns) if self._research_turns else "No research debate turns yet."
        risk = "\n".join(self._risk_turns) if self._risk_turns else "No risk debate turns yet."
        self.query_one("#debate-transcript", Static).update("Research debate\n" + research)
        self.query_one("#risk-debate-transcript", Static).update("Risk debate\n" + risk)

    def _render_decision(self) -> None:
        lines = ["Decision card"]
        if self._decision is not None:
            lines.extend(
                [
                    f"PM verdict: {self._decision.verdict}",
                    f"Size: {self._decision.approved_quantity_pct:.2f}%",
                    f"Reasoning: {self._decision.reasoning}",
                ]
            )
            if self._decision.lessons_applied:
                lines.append("Lessons: " + ", ".join(self._decision.lessons_applied))
        if self._gate is not None:
            if self._gate.passed:
                lines.append("Mandate: ✓ ALL_CHECKS")
            else:
                lines.append("Mandate: ✗ REJECTED")
                lines.extend(f"✗ {v.code}: {v.message}" for v in self._gate.violations)
                option_codes = [v.code for v in self._gate.violations if v.code.startswith(("OPTION_", "DEFINED_RISK", "EXPIRY"))]
                live_codes = [v.code for v in self._gate.violations if v.code.startswith(("LIVE_", "QUOTE_", "RECONCILIATION", "VENUE_"))]
                if option_codes:
                    lines.append("Option codes: " + ", ".join(option_codes))
                if live_codes:
                    lines.append("Live codes: " + ", ".join(live_codes))
        if self._order_text:
            lines.append(self._order_text)
        if self._fill_text:
            lines.append(self._fill_text)
        if len(lines) == 1:
            lines.append("Awaiting PM decision, mandate gate, and fill details.")
        self.query_one("#decision-card", Static).update("\n".join(lines))

    def _render_options_card(self) -> None:
        lines = ["OptionsAnalyst"]
        if self._options_report is None:
            rows = fetch_rows(
                "SELECT structured_json FROM reports WHERE run_id = ? AND lower(agent) LIKE '%options%' "
                "ORDER BY created_at DESC LIMIT 1",
                (self.run_id or "",),
            )
            if rows:
                with suppress(ValueError):
                    self._options_report = OptionsAnalystReport.model_validate_json(str(rows[0]["structured_json"]))
        if self._options_report is None:
            lines.append("Awaiting options report.")
        else:
            report = self._options_report
            lines.extend(
                [
                    f"IV {report.iv_regime} • expected move {report.expected_move_pct:.1f}%",
                    f"Skew: {report.skew_note}",
                    "Event risk: " + (", ".join(report.event_risk) if report.event_risk else "none"),
                    f"Confidence {report.confidence}%",
                ]
            )
        self.query_one("#options-card", Static).update("\n".join(lines))

    def _render_candidates(self) -> None:
        table = self.query_one("#option-candidates", DataTable)
        table.clear()
        if not self._candidates:
            table.add_row("—", "No option candidates loaded.", "—", "—", "—", "—", "—", "—")
            return
        for index, candidate in enumerate(self._candidates, start=1):
            candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or index)
            chosen = "★" if candidate_id == self._chosen_candidate_id else ""
            table.add_row(
                chosen,
                str(candidate.get("strategy") or "—"),
                _candidate_legs(candidate.get("legs")),
                money(candidate.get("net_premium")),
                money(candidate.get("max_loss")),
                _fmt_float(candidate.get("net_delta")),
                _fmt_float(candidate.get("net_vega")),
                _fmt_float(candidate.get("net_theta")),
                key=candidate_id,
            )

    def _render_live_panel(self) -> None:
        lines = ["Live panel"]
        orders = fetch_rows(
            "SELECT client_order_id, broker_order_id, venue, status, submitted_at, last_sync_at, raw_json "
            "FROM live_orders WHERE status IN ('working','partial') ORDER BY submitted_at DESC LIMIT 6"
        )
        reconciliation = fetch_rows(
            "SELECT ts, venue, ok, diff_json FROM reconciliations ORDER BY ts DESC, rowid DESC LIMIT 1"
        )
        if not orders and not reconciliation and not self._live_alerts:
            lines.append("paper only")
            lines.append("Press K for kill switch.")
            self.query_one("#live-panel", Static).update("\n".join(lines))
            return
        if orders:
            lines.append("Working/partial orders:")
            for row in orders:
                details = _live_order_details(row["raw_json"])
                lines.append(
                    f"{row['venue']} {row['status']} {str(row['client_order_id'])[:8]} {details}"
                )
        else:
            lines.append("No working live orders.")
        if reconciliation:
            row = reconciliation[0]
            ok = "OK" if row["ok"] else "FAILED"
            lines.append(f"Reconciliation {ok} {row['venue']} at {str(row['ts'])[:19]}")
            cash = _diff_value(row["diff_json"], "account_cash")
            headroom = _diff_value(row["diff_json"], "funding_cap_headroom")
            if cash is not None or headroom is not None:
                lines.append(f"RH cash {cash or '—'} • funding headroom {headroom or '—'}")
        if self._live_alerts:
            lines.append("Alerts:")
            lines.extend(reversed(self._live_alerts[-3:]))
        lines.append("Press K to jump to kill switch.")
        self.query_one("#live-panel", Static).update("\n".join(lines))

    def _render_feed(self) -> None:
        feed = "\n".join(reversed(self._feed[-12:])) if self._feed else "No live events yet."
        self.query_one("#live-feed", Static).update("Live feed\n" + feed)

    def _feed_line(self, message: str) -> None:
        append_limited(self._feed, f"{compact_time()} {message}")

    def _hydrate_latest_run(self) -> None:
        rows = fetch_rows(
            "SELECT run_id, symbol, status, cost_usd, created_at FROM runs "
            "ORDER BY COALESCE(finished_at, created_at, as_of) DESC LIMIT 1"
        )
        if not rows:
            return
        row = rows[0]
        self.run_id = str(row["run_id"])
        self.symbol = str(row["symbol"] or "—")
        self.status = str(row["status"] or "idle")
        self.total_cost = Decimal(str(row["cost_usd"] or "0"))
        self._candidates = _load_candidates(self.run_id)
        self._chosen_candidate_id = _load_chosen_candidate(self.run_id)

    def _venue_banner(self) -> str:
        rows = fetch_rows(
            "SELECT venue FROM live_orders ORDER BY submitted_at DESC LIMIT 1"
        )
        if rows:
            return f"LIVE {rows[0]['venue']}"
        return "PAPER"

    @staticmethod
    def _status_from_text(status: str) -> StageState:
        lowered = status.lower()
        if lowered in {"done", "complete", "completed", "success", "succeeded"}:
            return "done"
        if lowered in {"failed", "error"}:
            return "failed"
        if lowered in {"running", "started", "in_progress", "active"}:
            return "running"
        return "pending"

    @staticmethod
    def _stage_key(stage: str) -> str:
        lowered = stage.lower().replace("-", "_").replace(" ", "_")
        if "fetch" in lowered or "data" in lowered:
            return "fetch"
        if "risk" in lowered and "debate" in lowered:
            return "risk_debate"
        if "debate" in lowered:
            return "debate"
        if "research" in lowered and "manager" in lowered:
            return "research_manager"
        if "trader" in lowered or "trading" in lowered:
            return "trader"
        if "mandate" in lowered or ("gate" in lowered and "pm" not in lowered):
            return "mandate"
        if "pm" in lowered or "portfolio" in lowered:
            return "pm_gate"
        if "execut" in lowered or "fill" in lowered or "order" in lowered:
            return "execute"
        if "analy" in lowered or "agent" in lowered:
            return "analysts"
        return "analysts"


def _load_candidates(run_id: str | None) -> list[dict[str, Any]]:
    if not run_id:
        return []
    path = sentinel_home() / "runs" / run_id / "candidates.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if isinstance(item, dict):
            candidate = dict(item)
            candidate.setdefault("candidate_id", str(candidate.get("id") or index))
            candidates.append(candidate)
    return candidates


def _load_chosen_candidate(run_id: str) -> str | None:
    rows = fetch_rows(
        "SELECT structured_json FROM reports WHERE run_id = ? AND lower(agent) = 'trader' "
        "ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    )
    if not rows:
        return None
    try:
        payload = json.loads(str(rows[0]["structured_json"] or "{}"))
    except json.JSONDecodeError:
        return None
    candidate = payload.get("candidate_id") if isinstance(payload, dict) else None
    return str(candidate) if candidate is not None else None


def _candidate_legs(raw: object) -> str:
    if not isinstance(raw, list):
        return "—"
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        contract = cast(dict[str, Any], item.get("contract") or {})
        side = "B" if item.get("side") == "buy" else "S"
        strike = Decimal(str(contract.get("strike") or "0"))
        kind = str(contract.get("kind") or "?")[:1].upper()
        parts.append(f"{side}{item.get('contracts', 1)} {strike:g}{kind}")
    return " / ".join(parts) if parts else "—"


def _fmt_float(value: object) -> str:
    try:
        return f"{float(str(value)):+.2f}"
    except (TypeError, ValueError):
        return "—"


def _live_order_details(raw: object) -> str:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    filled = payload.get("filled_qty") or payload.get("filled_quantity")
    qty = payload.get("qty") or payload.get("quantity")
    symbol = payload.get("symbol")
    if filled is not None or qty is not None:
        return f"{symbol or ''} filled {filled or 0}/{qty or '?'}"
    return str(symbol or "")


def _diff_value(raw: object, key: str) -> str | None:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if value is None:
        for nested in payload.values():
            if isinstance(nested, dict) and nested.get(key) is not None:
                value = nested.get(key)
                break
    return str(value) if value is not None else None

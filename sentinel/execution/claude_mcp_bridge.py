"""Claude CLI bridge for Robinhood MCP.

Routes MCP tool calls through the ``claude`` CLI subprocess, which already has
the robinhood-trading MCP server authenticated. This is the fallback execution
path when no direct OAuth token is available for raw HTTP calls.

Usage:
    session = ClaudeMcpSession()
    result = await session.call_tool("get_accounts", {})
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Any

# All tools advertised by the Robinhood Agentic Trading MCP server.
# Populated from a live list_tools() probe on 2026-07-08.
_ROBINHOOD_TOOLS: list[str] = [
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_equity_orders",
    "get_equity_quotes",
    "get_equity_fundamentals",
    "get_equity_historicals",
    "get_equity_tradability",
    "get_option_positions",
    "get_option_orders",
    "get_option_quotes",
    "get_option_chains",
    "get_option_instruments",
    "get_option_historicals",
    "get_option_watchlist",
    "review_equity_order",
    "place_equity_order",
    "cancel_equity_order",
    "review_option_order",
    "place_option_order",
    "cancel_option_order",
    "get_pnl_trade_history",
    "get_realized_pnl",
    "get_indexes",
    "get_index_quotes",
    "get_watchlists",
    "get_watchlist_items",
    "get_popular_watchlists",
    "search",
    "get_earnings_calendar",
    "get_earnings_results",
    "get_scans",
    "run_scan",
    "create_scan",
    "update_scan_config",
    "update_scan_filters",
    "get_pnl_trade_history",
    "get_realized_pnl",
]

_MCP_PREFIX = "mcp__robinhood-trading__"

# How long to wait for a single claude CLI tool call (seconds).
# Order placement + review can take up to 30s on slow connections.
_TIMEOUT = 90.0


def find_claude_bin() -> str:
    """Locate the claude CLI binary."""
    candidates = [
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/bin/claude",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError(
        "claude CLI not found in PATH or common locations.\n"
        "Install Claude Code: https://claude.ai/code\n"
        "Then run: claude mcp add robinhood-trading --transport http "
        "https://agent.robinhood.com/mcp/trading"
    )


class ClaudeMcpSession:
    """Drop-in replacement for RhAgenticMcpSession that routes calls through claude CLI.

    The ``claude`` CLI must be installed and the robinhood-trading MCP server must
    be authenticated (run ``claude /mcp`` → select robinhood-trading → authenticate).

    This session is used automatically by RobinhoodAgenticBroker when no direct
    OAuth token is available for raw HTTP calls.
    """

    def __init__(self, claude_bin: str | None = None) -> None:
        self._claude_bin = claude_bin or find_claude_bin()
        # Non-None _token signals "authenticated" to callers that check session._token
        self._token: str = "claude-cli-bridge"

    async def list_tools(self) -> dict[str, Any]:
        """Return the known Robinhood MCP tool list (no subprocess needed)."""
        return {"tools": [{"name": t} for t in _ROBINHOOD_TOOLS]}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a Robinhood MCP tool via the claude CLI subprocess."""
        tool_id = f"{_MCP_PREFIX}{name}"
        args_json = json.dumps(arguments, indent=2)

        prompt = (
            f"Call the `{tool_id}` MCP tool with exactly these arguments:\n"
            f"```json\n{args_json}\n```\n\n"
            f"After calling the tool, output ONLY the tool result as raw JSON. "
            f"No explanation, no preamble — just the JSON object."
        )

        raw_output = await _run_claude(
            self._claude_bin,
            prompt=prompt,
            allowed_tool=tool_id,
        )
        result = _extract_json(raw_output)
        if not result:
            # Wrap raw text so callers get a dict (not a crash)
            result = {"raw_output": raw_output}
        return result


async def _run_claude(claude_bin: str, *, prompt: str, allowed_tool: str) -> str:
    """Spawn ``claude -p <prompt> --allowedTools <tool>`` and return stdout."""
    cmd = [
        claude_bin,
        "-p", prompt,
        "--allowedTools", allowed_tool,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Run from project root so .claude config / MCP settings are loaded
        cwd=os.path.expanduser("~"),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"claude CLI timed out after {_TIMEOUT}s calling {allowed_tool}"
        )

    if proc.returncode not in (0, None):
        err = (stderr or b"").decode(errors="replace")[:500]
        raise RuntimeError(
            f"claude CLI exited {proc.returncode} for {allowed_tool}: {err}"
        )

    return (stdout or b"").decode(errors="replace")


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from Claude's response text."""
    text = text.strip()

    # 1. Whole response is JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 2. JSON inside a markdown code block  ```json ... ``` or ``` ... ```
    for pattern in (
        r"```(?:json)?\s*(\{.*?\})\s*```",
        r"`(\{.*?\})`",
    ):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

    # 3. First bare {...} block in the text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return {}

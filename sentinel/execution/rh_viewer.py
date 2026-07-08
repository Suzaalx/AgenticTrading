"""Fetch live Robinhood account data via the Agentic Trading MCP session."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from sentinel.config.settings import RobinhoodAgenticSettings
from sentinel.execution.mcp_session import RhAgenticMcpSession

_DEFAULT_URL = "https://agent.robinhood.com/mcp/trading"


def build_session(settings: RobinhoodAgenticSettings | None = None) -> RhAgenticMcpSession:
    """Build a viewer session — token resolution order:
    1. ~/.sentinel/rh_tokens.json (written by ``sentinel auth``)
    2. RH_AGENTIC_MCP_TOKEN env var (manual override)
    """
    cfg = settings or RobinhoodAgenticSettings()
    url = os.environ.get(cfg.mcp_endpoint_env, _DEFAULT_URL)
    # RhAgenticMcpSession resolves token automatically from auth module + env
    return RhAgenticMcpSession(url=url)


async def fetch_accounts(session: RhAgenticMcpSession) -> list[dict[str, Any]]:
    result = await session.call_tool("get_accounts", {})
    return list(result.get("accounts") or [])


async def fetch_portfolio(session: RhAgenticMcpSession, account_number: str) -> dict[str, Any]:
    return await session.call_tool("get_portfolio", {"account_number": account_number})


async def fetch_equity_positions(
    session: RhAgenticMcpSession, account_number: str
) -> list[dict[str, Any]]:
    result = await session.call_tool("get_equity_positions", {"account_number": account_number})
    return list(result.get("positions") or [])


async def fetch_option_positions(
    session: RhAgenticMcpSession, account_number: str
) -> list[dict[str, Any]]:
    result = await session.call_tool(
        "get_option_positions", {"account_number": account_number, "nonzero": True}
    )
    return list(result.get("positions") or [])


def mask(account_number: str) -> str:
    return f"••••{account_number[-4:]}" if len(account_number) >= 4 else account_number


def dec(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value or default))
    except InvalidOperation:
        return Decimal("0")

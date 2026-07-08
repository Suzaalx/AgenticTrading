"""Robinhood Agentic Trading MCP HTTP/SSE session."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import cast

import httpx

JsonObject = dict[str, object]

_RPC_ID = 0

_DEFAULT_URL = "https://agent.robinhood.com/mcp/trading"


def _next_id() -> int:
    global _RPC_ID
    _RPC_ID += 1
    return _RPC_ID


class RhAgenticMcpSession:
    """Async MCP Streamable HTTP client for the Robinhood Agentic Trading server.

    Implements the subset of the MCP protocol that RobinhoodAgenticBroker uses:
    list_tools() and call_tool(name, arguments). Handles both application/json
    and text/event-stream (SSE) responses from the server.

    Token resolution order:
      1. Explicit ``token`` argument
      2. ~/.sentinel/rh_tokens.json (written by ``sentinel auth``)
      3. RH_AGENTIC_MCP_TOKEN environment variable
    """

    def __init__(self, url: str = _DEFAULT_URL, token: str | None = None) -> None:
        self._url = url
        if token is not None:
            self._token = token
        else:
            # Try auth module first, then env var
            try:
                from sentinel.execution.rh_auth import get_access_token
                self._token = get_access_token() or os.environ.get("RH_AGENTIC_MCP_TOKEN")
            except Exception:
                self._token = os.environ.get("RH_AGENTIC_MCP_TOKEN")

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def list_tools(self) -> JsonObject:
        """Return the server's tools/list response."""
        return await self._rpc("tools/list", {})

    async def call_tool(self, name: str, arguments: dict[str, object]) -> JsonObject:
        """Call an MCP tool and return the unwrapped result."""
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def _rpc(self, method: str, params: dict[str, object]) -> JsonObject:
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params, "id": _next_id()},
            separators=(",", ":"),
        )
        async with httpx.AsyncClient(headers=self._headers(), timeout=30.0) as client:
            response = await client.post(self._url, content=payload.encode())
        return _decode_response(response)


def _decode_response(response: httpx.Response) -> JsonObject:
    content_type = response.headers.get("content-type", "").lower()
    if "event-stream" in content_type:
        return _parse_sse(response.text)
    try:
        data = response.json()
    except Exception:
        return {}
    return _unwrap_rpc(data)


def _parse_sse(text: str) -> JsonObject:
    """Extract the last JSON-RPC result message from an SSE stream."""
    result: JsonObject = {}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            msg = json.loads(payload)
        except Exception:
            continue
        unwrapped = _unwrap_rpc(msg)
        if unwrapped:
            result = unwrapped
    return result


def _unwrap_rpc(data: object) -> JsonObject:
    """Unwrap JSON-RPC 2.0 envelope → result dict."""
    if not isinstance(data, dict):
        return {}
    data_map = cast(Mapping[str, object], data)
    if "error" in data_map:
        err = data_map["error"]
        raise RuntimeError(f"Robinhood MCP error: {err}")
    result = data_map.get("result")
    if result is None:
        return dict(data_map)
    if isinstance(result, dict):
        return dict(cast(Mapping[str, object], result))
    if isinstance(result, list):
        # tools/list can return a bare list
        return {"tools": result}
    return dict(data_map)

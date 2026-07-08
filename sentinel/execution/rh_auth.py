"""Robinhood MCP OAuth 2.0 PKCE authentication.

Implements the full authorization flow:
  1. Dynamic client registration (RFC 7591)
  2. Authorization code + PKCE (RFC 7636)
  3. Token exchange and storage (~/.sentinel/rh_tokens.json)
  4. Automatic token refresh

Usage:
    # CLI: sentinel auth
    # Programmatic:
    from sentinel.execution.rh_auth import get_access_token
    token = get_access_token()  # None if not authenticated
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

_TOKEN_FILE = Path.home() / ".sentinel" / "rh_tokens.json"
_PENDING_FILE = Path.home() / ".sentinel" / "rh_pending_auth.json"
_REGISTER_URL = "https://agent.robinhood.com/oauth/trading/register"
_AUTH_URL = "https://robinhood.com/oauth"
_TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
_REDIRECT_PORT = 54321
_REDIRECT_URI = f"http://localhost:{_REDIRECT_PORT}/callback"
_SCOPE = "internal"
_CLIENT_NAME = "Sentinel"


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


def load_tokens() -> dict[str, Any]:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text())
        except Exception:
            pass
    return {}


def save_tokens(tokens: dict[str, Any]) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    os.chmod(_TOKEN_FILE, 0o600)


def clear_tokens() -> None:
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()


def save_pending_auth(client_id: str, verifier: str) -> None:
    _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_FILE.write_text(json.dumps({"client_id": client_id, "verifier": verifier}))
    os.chmod(_PENDING_FILE, 0o600)


def load_pending_auth() -> dict[str, str] | None:
    if _PENDING_FILE.exists():
        try:
            data = json.loads(_PENDING_FILE.read_text())
            if data.get("client_id") and data.get("verifier"):
                return {"client_id": str(data["client_id"]), "verifier": str(data["verifier"])}
        except Exception:
            pass
    return None


def clear_pending_auth() -> None:
    if _PENDING_FILE.exists():
        _PENDING_FILE.unlink()


# ---------------------------------------------------------------------------
# Token access (sync, safe to call from anywhere)
# ---------------------------------------------------------------------------


def get_access_token() -> str | None:
    """Return a valid access token, auto-refreshing if expired. None if not authed."""
    tokens = load_tokens()
    if not tokens:
        return None

    expires_at = float(tokens.get("expires_at", 0))
    if time.time() < expires_at - 60:
        return str(tokens["access_token"])

    # Try refresh
    refresh_tok = tokens.get("refresh_token")
    client_id = tokens.get("client_id")
    if refresh_tok and client_id:
        try:
            resp = httpx.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_tok,
                    "client_id": client_id,
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            new = resp.json()
            new["client_id"] = client_id
            new.setdefault("refresh_token", refresh_tok)
            new["expires_at"] = time.time() + float(new.get("expires_in", 3600))
            save_tokens(new)
            return str(new["access_token"])
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------


class _CallbackCapture:
    """Single-use local HTTP server that captures the OAuth callback code."""

    def __init__(self) -> None:
        self._code: str | None = None
        self._error: str | None = None
        self._done = threading.Event()

    def serve(self) -> None:
        capture = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                codes = params.get("code", [])
                errors = params.get("error", [])

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()

                if codes:
                    capture._code = codes[0]
                    self.wfile.write(
                        b"<html><body style='font-family:sans-serif;padding:2em'>"
                        b"<h2>Authenticated!</h2>"
                        b"<p>Return to your terminal. You can close this tab.</p>"
                        b"</body></html>"
                    )
                else:
                    capture._error = errors[0] if errors else "unknown error"
                    self.wfile.write(
                        b"<html><body style='font-family:sans-serif;padding:2em'>"
                        b"<h2>Authentication failed</h2>"
                        b"<p>Check your terminal for details.</p>"
                        b"</body></html>"
                    )
                capture._done.set()

            def log_message(self, format: str, *args: object) -> None:  # type: ignore[override]
                pass

        server = HTTPServer(("localhost", _REDIRECT_PORT), _Handler)
        server.handle_request()

    def wait(self, timeout: float = 120.0) -> str:
        if not self._done.wait(timeout=timeout):
            raise TimeoutError("Timed out waiting for Robinhood OAuth callback")
        if self._error:
            raise RuntimeError(f"Robinhood OAuth error: {self._error}")
        assert self._code is not None
        return self._code


# ---------------------------------------------------------------------------
# Full OAuth flow (async)
# ---------------------------------------------------------------------------


async def exchange_saved_code(auth_code: str) -> str:
    """Exchange an authorization code using the saved pending auth state.

    Use this after manually copying the ``?code=`` from the browser redirect URL.
    """
    pending = load_pending_auth()
    if not pending:
        raise RuntimeError(
            "No pending auth found. Run 'sentinel auth' first to generate the URL."
        )
    client_id = pending["client_id"]
    verifier = pending["verifier"]
    tokens = await _exchange_code(auth_code, verifier, client_id)
    tokens["client_id"] = client_id
    tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 3600))
    save_tokens(tokens)
    clear_pending_auth()
    return str(tokens["access_token"])


async def run_oauth_flow(*, quiet: bool = False) -> str:
    """Run the full PKCE flow. Opens browser, waits for callback, stores tokens.

    Returns the access token on success.
    """
    # 1. Dynamic client registration
    if not quiet:
        print("Registering Sentinel with Robinhood...")
    client_id = await _register_client()

    # 2. PKCE
    verifier, challenge = _pkce_pair()

    # 3. Auth URL
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": _SCOPE,
    }
    auth_url = f"{_AUTH_URL}?{urlencode(params)}"

    # Save pending state so --code can complete the flow if the callback times out
    save_pending_auth(client_id, verifier)

    # 4. Local callback server
    capture = _CallbackCapture()
    thread = threading.Thread(target=capture.serve, daemon=True)
    thread.start()

    if not quiet:
        print(f"\nOpening Robinhood in your browser to authenticate...")
        print(f"URL:\n  {auth_url}\n")
        print("After logging in, Robinhood will redirect to localhost:54321.")
        print("If that fails (connection refused), copy the '?code=...' from the URL bar")
        print("and run:  sentinel auth --code <paste-code-here>\n")
    webbrowser.open(auth_url)

    # 5. Wait for code (non-blocking on the event loop)
    loop = asyncio.get_running_loop()
    auth_code = await loop.run_in_executor(None, lambda: capture.wait(120.0))

    if not quiet:
        print("Authorization code received. Exchanging for tokens...")

    # 6. Exchange code for tokens
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "code_verifier": verifier,
                "redirect_uri": _REDIRECT_URI,
                "client_id": client_id,
            },
        )
    resp.raise_for_status()
    tokens: dict[str, Any] = resp.json()
    tokens["client_id"] = client_id
    tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 3600))

    save_tokens(tokens)
    if not quiet:
        print(f"Tokens saved to {_TOKEN_FILE}")

    return str(tokens["access_token"])


async def _register_client() -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _REGISTER_URL,
            json={
                "client_name": _CLIENT_NAME,
                "redirect_uris": [_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": _SCOPE,
            },
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Robinhood client registration failed: {resp.status_code} {resp.text[:200]}"
        )
    data: dict[str, Any] = resp.json()
    client_id = data.get("client_id")
    if not client_id:
        raise RuntimeError(f"Registration response missing client_id: {data}")
    return str(client_id)

"""Global test configuration: block outbound network and isolate Sentinel home."""

from __future__ import annotations

from pathlib import Path

import pytest

_NETWORK_GUARD_INSTALLED = False


def pytest_configure() -> None:
    global _NETWORK_GUARD_INSTALLED
    if _NETWORK_GUARD_INSTALLED:
        return
    import socket

    true_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) and address else None
        if host in {"127.0.0.1", "::1", "localhost"}:
            return true_connect(self, address)
        raise OSError("Network disabled by Sentinel tests")

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    _NETWORK_GUARD_INSTALLED = True


@pytest.fixture(autouse=True)
def _disable_network_and_isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SENTINEL_HOME", str(tmp_path / ".sentinel"))

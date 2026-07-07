from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sentinel.__main__ import app


def test_doctor_reports_options_checks_without_network(monkeypatch) -> None:
    monkeypatch.delenv("SENTINEL_DOCTOR_NETWORK", raising=False)

    result = CliRunner().invoke(app, ["doctor", "--root", str(Path.cwd())])

    assert result.exit_code == 0, result.output
    assert "Options readiness" in result.output
    assert "options tables migrated: yes" in result.output
    assert "option chain fetch: skipped" in result.output
    assert "^IRX risk-free rate: skipped" in result.output
    assert "kill-switch path writable:" in result.output

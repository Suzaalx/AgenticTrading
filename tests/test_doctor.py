from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sentinel.__main__ import app


def test_doctor_exits_zero() -> None:
    result = CliRunner().invoke(app, ["doctor", "--root", str(Path.cwd())])
    assert result.exit_code == 0, result.output
    assert "Sentinel doctor" in result.output
    assert "DB writable: yes" in result.output
    assert "config.toml valid: yes" in result.output
    assert "mandate.toml valid: yes" in result.output

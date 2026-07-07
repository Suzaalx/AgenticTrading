from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sentinel.__main__ import app


def test_doctor_live_redacts_secret_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(Path.cwd() / "mandate.toml", root / "mandate.toml")
    (root / "config.toml").write_text("[execution.robinhood]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv("ROBINHOOD_API_KEY", "super-secret-api-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", "super-secret-private-key")

    result = CliRunner().invoke(app, ["doctor", "--root", str(root), "--live"])

    assert result.exit_code == 0, result.output
    assert "Live readiness" in result.output
    assert "present" in result.output
    assert "super-secret-api-key" not in result.output
    assert "super-secret-private-key" not in result.output

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sentinel.__main__ import app
from sentinel.risk.audit import read_audit
from sentinel.store.db import connect, default_db_path, run_migrations


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(Path.cwd() / "mandate.toml", root / "mandate.toml")
    (root / "config.toml").write_text("", encoding="utf-8")
    return root


def _seed_crypto_history(pnl: bool = True, runs: int = 30) -> None:
    conn = connect(default_db_path())
    try:
        run_migrations(conn)
        for index in range(runs):
            conn.execute(
                """
                INSERT INTO runs
                (run_id, symbol, as_of, mode, status, created_at)
                VALUES (?, 'BTC-USD', ?, 'paper', 'completed', ?)
                """,
                (f"run-{index}", f"2026-01-{(index % 28) + 1:02d}", "2026-01-01"),
            )
        if pnl:
            conn.execute(
                "INSERT INTO equity_curve (ts, equity, cash, day_pnl) VALUES ('2026-01-01', 1, 1, 0)"
            )
        conn.commit()
    finally:
        conn.close()


def _stage_enabled(root: Path, flag: str) -> bool:
    with (root / "mandate.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return bool(data["mandate"]["live"][flag])


def test_graduate_crypto_all_met_flips_flag_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _seed_crypto_history()
    monkeypatch.setenv("ROBINHOOD_API_KEY", "raw-api-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", "raw-private-key")

    result = CliRunner().invoke(
        app,
        ["graduate", "crypto", "--root", str(root)],
        input="I UNDERSTAND LIVE CRYPTO\n",
    )

    assert result.exit_code == 0, result.output
    assert _stage_enabled(root, "crypto_stage_enabled")
    assert any(row["kind"] == "live_stage_changed" for row in read_audit())


@pytest.mark.parametrize(
    ("runs", "pnl", "api_key", "private_key", "expected"),
    [
        (29, True, "key", "priv", "30 completed paper runs"),
        (30, False, "key", "priv", "P&L history is absent"),
        (30, True, "", "priv", "ROBINHOOD_API_KEY is absent"),
        (30, True, "key", "", "ROBINHOOD_PRIVATE_KEY is absent"),
    ],
)
def test_graduate_crypto_each_unmet_prerequisite_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runs: int,
    pnl: bool,
    api_key: str,
    private_key: str,
    expected: str,
) -> None:
    root = _root(tmp_path)
    _seed_crypto_history(pnl=pnl, runs=runs)
    if api_key:
        monkeypatch.setenv("ROBINHOOD_API_KEY", api_key)
    if private_key:
        monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", private_key)

    result = CliRunner().invoke(app, ["graduate", "crypto", "--root", str(root)])

    assert result.exit_code == 1
    assert expected in result.output
    assert not _stage_enabled(root, "crypto_stage_enabled")


def test_demote_flips_flag_off_without_confirmation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    text = (root / "mandate.toml").read_text(encoding="utf-8").replace(
        "crypto_stage_enabled = false",
        "crypto_stage_enabled = true",
    )
    (root / "mandate.toml").write_text(text, encoding="utf-8")

    result = CliRunner().invoke(app, ["demote", "crypto", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert not _stage_enabled(root, "crypto_stage_enabled")

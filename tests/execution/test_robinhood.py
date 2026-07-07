from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.config.settings import RobinhoodSettings, load_settings
from sentinel.execution.robinhood import RobinhoodCryptoBroker, sign_headers

PEM = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB
-----END PRIVATE KEY-----"""


def test_constructor_disabled_raises() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        RobinhoodCryptoBroker(RobinhoodSettings(enabled=False))


def test_crypto_signing_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBINHOOD_API_KEY", "rh-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", PEM)

    headers = sign_headers(
        RobinhoodSettings(enabled=True),
        "POST",
        "/api/v1/crypto/trading/orders/",
        '{"client_order_id":"client-1"}',
        timestamp=1783440000,
        clock=lambda: datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
    )

    assert headers == {
        "x-api-key": "rh-key",
        "x-signature": "e1mN2MllhaLh8M/ewdNO7fzKK8mA32YQFkLywIaZO4r9vZBpbsuFygN55YUQIVNRia4X33oa3TtxVodMtuDMCQ==",
        "x-timestamp": "1783440000",
    }


def test_robinhood_settings_default_disabled(tmp_path) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    settings = load_settings(tmp_path)

    assert settings.execution.robinhood.enabled is False

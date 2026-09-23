from __future__ import annotations

from sentinel.config.settings import load_settings


def test_load_settings_accepts_optional_role_models(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        """
[llm]
provider = "openai"
quick_model = "quick-default"
deep_model = "deep-default"

[llm.role_models]
market_analyst = "cheap-market-model"
research_manager = "judge-model"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.llm.quick_model == "quick-default"
    assert settings.llm.role_models == {
        "market_analyst": "cheap-market-model",
        "research_manager": "judge-model",
    }


def test_load_settings_keeps_role_models_backward_compatible(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        """
[llm]
provider = "openai"
quick_model = "quick-default"
deep_model = "deep-default"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.llm.role_models == {}


def test_debate_enabled_defaults_true_and_reads_toml_and_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SENTINEL_PIPELINE__DEBATE_ENABLED", raising=False)
    assert load_settings(tmp_path).pipeline.debate_enabled is True

    (tmp_path / "config.toml").write_text("[pipeline]\ndebate_enabled = false\n", encoding="utf-8")
    assert load_settings(tmp_path).pipeline.debate_enabled is False

    monkeypatch.setenv("SENTINEL_PIPELINE__DEBATE_ENABLED", "true")
    assert load_settings(tmp_path).pipeline.debate_enabled is True

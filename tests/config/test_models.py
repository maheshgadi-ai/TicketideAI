from pathlib import Path

import pytest

from ticketideai.config import ConfigurationError, ModelsConfig, Settings, load_models_config

CONFIG = ModelsConfig.model_validate(
    {
        "default": {"provider": "openai", "model": "base-model", "max_tokens": 1000},
        "roles": {
            "planner": {
                "provider": "openai",
                "model": "big-model",
                "reasoning_effort": "high",
            },
            "responder": {"provider": "anthropic", "model": "claude-x", "max_tokens": 5000},
        },
    }
)


def resolve(role: str, monkeypatch: pytest.MonkeyPatch | None = None, **env: str):
    if monkeypatch:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return CONFIG.resolve(role, Settings())


# --- Role routing -----------------------------------------------------------


def test_role_uses_its_own_entry() -> None:
    spec = resolve("responder")
    assert (spec.provider, spec.model, spec.max_tokens) == ("anthropic", "claude-x", 5000)


def test_role_inherits_unset_params_from_default() -> None:
    spec = resolve("planner")
    assert spec.model == "big-model"
    assert spec.reasoning_effort == "high"
    assert spec.max_tokens == 1000


def test_unknown_role_falls_back_to_default() -> None:
    assert resolve("judge").label == "openai:base-model"


def test_role_lookup_is_case_insensitive() -> None:
    assert resolve("RESPONDER").model == "claude-x"


# --- Override precedence ------------------------------------------------------


def test_role_env_override_beats_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = resolve("responder", monkeypatch, MODEL__RESPONDER="openai:gpt-override")
    assert spec.label == "openai:gpt-override"
    assert spec.max_tokens == 5000  # params kept from the role entry


def test_role_env_override_beats_default_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = resolve(
        "judge",
        monkeypatch,
        MODEL__JUDGE="anthropic:judge-model",
        LLM_PROVIDER="openai",
        OPENAI_MODEL="env-default",
    )
    assert spec.label == "anthropic:judge-model"


def test_provider_env_overrides_default_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env")
    assert resolve("judge").label == "anthropic:claude-env"  # falls back to default
    assert resolve("responder").model == "claude-x"  # explicit roles keep their entry
    assert resolve("default").max_tokens == 1000


def test_model_env_without_provider_applies_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve("judge", monkeypatch, OPENAI_MODEL="gpt-env").label == "openai:gpt-env"


def test_model_default_env_beats_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = resolve(
        "default",
        monkeypatch,
        MODEL__DEFAULT="anthropic:claude-d",
        LLM_PROVIDER="openai",
        OPENAI_MODEL="gpt-env",
    )
    assert spec.label == "anthropic:claude-d"


def test_switching_provider_without_model_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="ANTHROPIC_MODEL"):
        resolve("judge", monkeypatch, LLM_PROVIDER="anthropic")


@pytest.mark.parametrize("bad", ["claude-x", "google:gemini", "openai:", ":model"])
def test_malformed_role_override_rejected(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    with pytest.raises(ConfigurationError, match="MODEL__RESPONDER"):
        resolve("responder", monkeypatch, MODEL__RESPONDER=bad)


# --- Loading ----------------------------------------------------------------


def test_custom_config_path(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("default:\n  provider: anthropic\n  model: only-model\n")
    assert load_models_config(path).default.model == "only-model"


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_models_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "content",
    [
        "roles: {}\n",  # no default
        "default: {provider: mistral, model: m}\n",  # unknown provider
        "default: {provider: openai, model: m, temprature: 1}\n",  # typo'd param
        "default: [unclosed\n",  # bad YAML
    ],
)
def test_invalid_config_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(content)
    with pytest.raises(ConfigurationError, match="Invalid models config"):
        load_models_config(path)


def test_pricing_lookup() -> None:
    cfg = ModelsConfig.model_validate(
        {
            "default": {"provider": "openai", "model": "m"},
            "pricing": {"m": {"input_per_mtok": 1.5, "output_per_mtok": 6}},
        }
    )
    assert cfg.price_for("m").input_per_mtok == 1.5
    assert not cfg.price_for("unknown").known


# --- Capability validation ----------------------------------------------------

CAPS = {
    "astra": {
        "provider": "openai",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "temperature_efforts": [],
    },
    "sol": {
        "provider": "openai",
        "efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
        "temperature_efforts": ["none"],
    },
}


def config_with(default: dict, roles: dict | None = None) -> ModelsConfig:
    return ModelsConfig.model_validate(
        {"default": default, "roles": roles or {}, "capabilities": CAPS}
    )


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_documented_efforts_accepted(effort: str) -> None:
    cfg = config_with({"provider": "openai", "model": "astra", "reasoning_effort": effort})
    assert cfg.resolve("default", Settings()).reasoning_effort == effort


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"model": "astra", "reasoning_effort": "none"}, "not supported"),
        ({"model": "sol", "reasoning_effort": "minimal"}, "not supported"),
        ({"model": "astra", "temperature": 0.2}, "never"),
        ({"model": "astra", "temperature": 0.2, "reasoning_effort": "low"}, "never"),
        ({"model": "sol", "temperature": 0.2}, "effective effort: 'medium'"),
        ({"model": "sol", "temperature": 0.2, "reasoning_effort": "low"}, "temperature"),
    ],
)
def test_invalid_combinations_rejected_at_load(spec: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        config_with({"provider": "openai", **spec})


def test_temperature_allowed_with_effort_none() -> None:
    cfg = config_with(
        {"provider": "openai", "model": "sol", "reasoning_effort": "none", "temperature": 0.2}
    )
    assert cfg.resolve("default", Settings()).temperature == 0.2


def test_provider_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="is served by openai, not anthropic"):
        config_with({"provider": "anthropic", "model": "sol"})


def test_role_checked_with_inherited_params() -> None:
    with pytest.raises(ValueError, match="role 'planner'"):
        config_with(
            {"provider": "openai", "model": "sol", "reasoning_effort": "none"},
            {"planner": {"provider": "openai", "model": "astra"}},  # inherits effort=none
        )


def test_env_override_validated_at_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_with(
        {"provider": "openai", "model": "sol"},
        {"responder": {"provider": "openai", "model": "sol", "reasoning_effort": "none"}},
    )
    monkeypatch.setenv("MODEL__RESPONDER", "openai:astra")
    with pytest.raises(ConfigurationError, match=r"role 'responder'.*not supported"):
        cfg.resolve("responder", Settings())


def test_unlisted_models_not_validated() -> None:
    cfg = config_with({"provider": "openai", "model": "future-model", "reasoning_effort": "x"})
    assert cfg.resolve("default", Settings()).reasoning_effort == "x"


def test_packaged_capabilities_match_openai_docs() -> None:
    caps = load_models_config().capabilities
    assert caps["gpt-6-astra"].efforts == ["low", "medium", "high", "xhigh", "max"]
    for model in ("gpt-6-sol", "gpt-6-luna"):
        assert caps[model].efforts == ["none", "low", "medium", "high", "xhigh", "max"]
        assert caps[model].default_effort == "medium"
        assert caps[model].temperature_efforts == ["none"]

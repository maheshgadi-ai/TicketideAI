import pytest
from pydantic import ValidationError

from ticketideai.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.llm_provider is None
    assert s.model == {}
    assert s.models_config_path is None
    assert not s.tracing_enabled


def test_role_overrides_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL__RESPONDER", "anthropic:some-model")
    monkeypatch.setenv("MODEL__Judge", "openai:other")
    assert Settings().model == {"responder": "anthropic:some-model", "judge": "openai:other"}


def test_dotenv_file_is_loaded(tmp_path) -> None:
    (tmp_path / ".env").write_text("OPENAI_MODEL=from-dotenv\nMODEL__RESPONDER=openai:x\n")
    s = Settings()
    assert s.openai_model == "from-dotenv"
    assert s.model == {"responder": "openai:x"}


def test_api_key_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    s = Settings()
    assert s.api_key("anthropic") == "sk-ant"
    assert s.api_key("openai") is None


def test_tracing_requires_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    assert not Settings().tracing_enabled
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    assert not Settings().tracing_enabled
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert Settings().tracing_enabled


def test_invalid_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-very-secret")
    assert "sk-very-secret" not in repr(Settings())


def test_env_example_template_has_no_placeholder_keys(tmp_path, monkeypatch) -> None:
    """Copying .env.example to .env must not make fake keys look real (or enable tracing)."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text())
    s = Settings()
    assert s.api_key("openai") is None and s.api_key("anthropic") is None
    assert not s.tracing_enabled

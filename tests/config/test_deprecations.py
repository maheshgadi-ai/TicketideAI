"""No config may reference a model on OpenAI's deprecations list."""

from pathlib import Path

import pytest
import yaml

from ticketideai.config import ConfigurationError, Settings, load_models_config
from ticketideai.config.models import CONFIG_DIR, DEPRECATIONS_PATH

DEPRECATED = yaml.safe_load(DEPRECATIONS_PATH.read_text())["deprecated"]
CONFIG_FILES = sorted(
    p for p in CONFIG_DIR.glob("*.yaml") if p != DEPRECATIONS_PATH and p.name != "policy.yaml"
)


def referenced_models(path: Path) -> set[str]:
    """Every model ID a config file mentions: routing entries and catalog keys."""
    data = yaml.safe_load(path.read_text()) or {}
    models = set(data.get("capabilities", {})) | set(data.get("pricing", {}))
    if "default" in data:
        models.add(data["default"]["model"])
    models |= {spec["model"] for spec in data.get("roles", {}).values()}
    return models


def test_config_files_found() -> None:
    names = {p.name for p in CONFIG_FILES}
    assert {"models.yaml", "models.budget.yaml", "model_catalog.yaml"} <= names


@pytest.mark.parametrize("path", CONFIG_FILES, ids=lambda p: p.name)
def test_no_config_references_a_deprecated_model(path: Path) -> None:
    used = referenced_models(path)
    assert used, f"{path.name} references no models; did the format change?"
    offenders = {m: DEPRECATED[m] for m in used & set(DEPRECATED)}
    assert not offenders, f"{path.name} uses deprecated models: {offenders}"


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5-nano", "gpt-4o-mini", "gpt-4.1-mini"])
def test_list_covers_known_deprecations(model: str) -> None:
    assert model in DEPRECATED


def test_replacements_are_not_themselves_deprecated() -> None:
    replacements = {d["replacement"] for d in DEPRECATED.values()} - {None}
    assert not replacements & set(DEPRECATED)


def test_loader_rejects_profile_using_deprecated_model(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("default: {provider: openai, model: gpt-4o-mini}\n")
    with pytest.raises(ConfigurationError, match=r"gpt-4o-mini is deprecated.*use gpt-6-luna"):
        load_models_config(path)


def test_env_override_to_deprecated_model_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL__RESPONDER", "openai:gpt-5-nano")
    with pytest.raises(ConfigurationError, match="gpt-5-nano is deprecated"):
        load_models_config().resolve("responder", Settings())

"""The shipped routing profiles: default (models.yaml) and budget (models.budget.yaml)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ticketideai.config import ModelsConfig, Settings, load_models_config
from ticketideai.config.models import CONFIG_DIR, DEFAULT_MODELS_PATH
from ticketideai.interfaces.cli import app

BUDGET = CONFIG_DIR / "models.budget.yaml"
PROFILES = [DEFAULT_MODELS_PATH, BUDGET]
ROLES = {"planner", "responder", "judge"}
MIN_PLANNER_TIER = 2


def resolved(path: Path) -> dict[str, str]:
    cfg = load_models_config(path)
    return {role: cfg.resolve(role, Settings()).label for role in ["default", *cfg.roles]}


def test_default_profile_matches_spec() -> None:
    assert resolved(DEFAULT_MODELS_PATH) == {
        "default": "openai:gpt-6-sol",
        "planner": "anthropic:claude-opus-5-5",
        "responder": "openai:gpt-6-luna",
        "judge": "openai:gpt-6-astra",
    }


def test_budget_profile_matches_spec() -> None:
    assert resolved(BUDGET) == {
        "default": "openai:gpt-6-sol",
        "planner": "anthropic:claude-sonnet-5-5",
        "responder": "openai:gpt-6-luna",
        "judge": "openai:gpt-6-sol",
    }


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.name)
def test_role_rules_shared_by_all_profiles(path: Path) -> None:
    cfg = load_models_config(path)
    spec = {r: cfg.resolve(r, Settings()) for r in cfg.roles}
    assert set(spec) == ROLES
    assert spec["judge"].label not in {spec["planner"].label, spec["responder"].label}


def _price_key(cfg: ModelsConfig, model: str) -> tuple[float, float]:
    price = cfg.price_for(model)
    return (price.output_per_mtok, price.input_per_mtok)


def _dominated_by(cfg: ModelsConfig, model: str) -> str | None:
    """Another same-provider model at >= tier and <= both prices, strictly better somewhere."""
    mine, my_price = cfg.capabilities[model], cfg.price_for(model)
    for other, caps in cfg.capabilities.items():
        price = cfg.price_for(other)
        if other == model or caps.provider != mine.provider or not price.known:
            continue
        if None in (caps.tier, mine.tier):
            continue
        no_worse = (
            caps.tier >= mine.tier
            and price.input_per_mtok <= my_price.input_per_mtok
            and price.output_per_mtok <= my_price.output_per_mtok
        )
        better = (caps.tier, -price.input_per_mtok, -price.output_per_mtok) != (
            mine.tier,
            -my_price.input_per_mtok,
            -my_price.output_per_mtok,
        )
        if no_worse and better:
            return other
    return None


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.name)
def test_no_routed_model_is_beaten_on_both_tier_and_price(path: Path) -> None:
    cfg = load_models_config(path)
    for role in ["default", *cfg.roles]:
        model = cfg.resolve(role, Settings()).model
        winner = _dominated_by(cfg, model)
        assert winner is None, f"{path.name}: {role} uses {model}, but {winner} is no worse"


def test_terra_is_dominated_by_sol() -> None:
    assert _dominated_by(load_models_config(BUDGET), "gpt-5.6-terra") == "gpt-6-sol"


def test_budget_roles_are_cheapest_allowed() -> None:
    cfg = load_models_config(BUDGET)
    spec = {r: cfg.resolve(r, Settings()) for r in cfg.roles}
    eligible = [
        m for m in cfg.capabilities if cfg.price_for(m).known and _dominated_by(cfg, m) is None
    ]

    def cheapest(candidates: list[str]) -> str:
        return min(candidates, key=lambda m: _price_key(cfg, m))

    assert spec["responder"].model == cheapest(eligible)
    planner_pool = [m for m in eligible if cfg.capabilities[m].tier >= MIN_PLANNER_TIER]
    assert cfg.price_for(spec["planner"].model) == cfg.price_for(cheapest(planner_pool))
    judge_pool = [m for m in eligible if m not in {spec["planner"].model, spec["responder"].model}]
    assert spec["judge"].model == cheapest(judge_pool)


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.name)
def test_every_routed_model_is_validated_and_priced(path: Path) -> None:
    cfg = load_models_config(path)
    for role in ["default", *cfg.roles]:
        model = cfg.resolve(role, Settings()).model
        assert cfg.capabilities_for(model), f"{path.name}: {model} has no capabilities entry"
        assert cfg.price_for(model).known, f"{path.name}: {model} has no price"


def test_budget_selected_with_models_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(BUDGET))
    result = CliRunner().invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert "anthropic:claude-sonnet-5-5" in result.output
    assert "openai:gpt-6-luna" in result.output


def test_profile_cannot_shadow_catalog_with_bad_capabilities(tmp_path: Path) -> None:
    """A profile may override a catalog entry, and the override is what gets checked."""
    path = tmp_path / "p.yaml"
    path.write_text(
        "default: {provider: openai, model: gpt-6-sol, reasoning_effort: low}\n"
        "capabilities:\n  gpt-6-sol: {provider: openai, efforts: [high]}\n"
    )
    with pytest.raises(ValueError, match="reasoning_effort='low' not supported"):
        load_models_config(path)

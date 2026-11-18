"""Load `models.yaml`, validate it, and resolve which model serves each role."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ticketideai.config.settings import ProviderName, Settings

CONFIG_DIR = Path(__file__).parent
DEFAULT_MODELS_PATH = CONFIG_DIR / "models.yaml"
PROFILE_PATHS = {"default": DEFAULT_MODELS_PATH, "budget": CONFIG_DIR / "models.budget.yaml"}
CATALOG_PATH = CONFIG_DIR / "model_catalog.yaml"
DEPRECATIONS_PATH = CONFIG_DIR / "deprecated_models.yaml"
DEFAULT_ROLE = "default"


class ConfigurationError(ValueError):
    """Raised when configuration is missing or invalid."""


class ModelParams(BaseModel):
    """Optional generation parameters. `None` means inherit or use the provider default."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None


class ModelSpec(ModelParams):
    """A provider, a model, and optional params for one role."""

    provider: ProviderName
    model: str

    @property
    def label(self) -> str:
        """Return `provider:model`."""
        return f"{self.provider}:{self.model}"


class ModelCapabilities(BaseModel):
    """What a model accepts, taken from the provider's model docs.

    `None` for a list means "not documented, so not checked".
    `temperature_efforts` lists the effort levels at which `temperature` is allowed;
    an empty list means never. `tier` ranks capability within one provider's lineup
    (higher is stronger); it isn't comparable across providers.
    """

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    tier: int | None = Field(default=None, ge=1)
    efforts: list[str] | None = None
    default_effort: str | None = None
    temperature_efforts: list[str] | None = None

    def problems(self, spec: ModelSpec) -> list[str]:
        """Return human-readable reasons `spec` is invalid for this model (empty if OK)."""
        found: list[str] = []
        if spec.provider != self.provider:
            found.append(f"{spec.model} is served by {self.provider}, not {spec.provider}")
        effort = spec.reasoning_effort
        if effort is not None and self.efforts is not None and effort not in self.efforts:
            found.append(f"reasoning_effort={effort!r} not supported; use one of {self.efforts}")
        if spec.temperature is not None and self.temperature_efforts is not None:
            effective = effort or self.default_effort
            if effective not in self.temperature_efforts:
                allowed = self.temperature_efforts or "never"
                found.append(
                    f"temperature is only accepted when reasoning_effort is in {allowed} "
                    f"(effective effort: {effective!r})"
                )
        return found


class Pricing(BaseModel):
    """Prices in USD per one million tokens. Zero means unknown."""

    input_per_mtok: float = Field(default=0.0, ge=0.0)
    output_per_mtok: float = Field(default=0.0, ge=0.0)

    @property
    def known(self) -> bool:
        """True when at least one price is configured."""
        return self.input_per_mtok > 0 or self.output_per_mtok > 0


class Deprecation(BaseModel):
    """A model the provider has deprecated, from its deprecations page."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    shutdown: str
    replacement: str | None = None


class ModelsConfig(BaseModel):
    """A routing profile merged with the shared model catalog and deprecations."""

    model_config = ConfigDict(extra="forbid")

    default: ModelSpec
    roles: dict[str, ModelSpec] = Field(default_factory=dict)
    capabilities: dict[str, ModelCapabilities] = Field(default_factory=dict)
    pricing: dict[str, Pricing] = Field(default_factory=dict)
    deprecated: dict[str, Deprecation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_yaml_roles(self) -> ModelsConfig:
        """Reject invalid model/param combinations in the file itself."""
        for role in [DEFAULT_ROLE, *self.roles]:
            spec = self.default if role == DEFAULT_ROLE else self._merge(self.roles[role])
            self._validate(role, spec, ValueError)
        return self

    def resolve(self, role: str, settings: Settings) -> ModelSpec:
        """Return the validated model spec for `role` after applying env overrides.

        Precedence, highest first:
        1. `MODEL__<ROLE>=provider:model` (provider and model only).
        2. The role's entry in `roles`.
        3. The `default` entry, after `MODEL__DEFAULT` or `LLM_PROVIDER` /
           `OPENAI_MODEL` / `ANTHROPIC_MODEL` overrides.

        Params a role doesn't set are inherited from the default entry.

        Raises:
            ConfigurationError: If an override is malformed or the final model
                doesn't accept the configured params.
        """
        role = role.lower()
        default = self._resolved_default(settings)
        if role == DEFAULT_ROLE:
            spec = default
        else:
            spec = self._merge(self.roles.get(role, default), default)
            override = settings.model.get(role)
            if override:
                ref = parse_model_ref(override, f"MODEL__{role.upper()}")
                spec = spec.model_copy(update=ref)
        self._validate(role, spec, ConfigurationError)
        return spec

    def capabilities_for(self, model: str) -> ModelCapabilities | None:
        """Return the documented capabilities of a model ID, if listed."""
        return self.capabilities.get(model)

    def price_for(self, model: str) -> Pricing:
        """Return pricing for a model ID, or an empty (unknown) price."""
        return self.pricing.get(model, Pricing())

    def _merge(self, spec: ModelSpec, default: ModelSpec | None = None) -> ModelSpec:
        default = default or self.default
        inherited = {k: v for k, v in default.model_dump().items() if v is not None}
        return ModelSpec.model_validate({**inherited, **spec.model_dump(exclude_none=True)})

    def _validate(self, role: str, spec: ModelSpec, error: type[ValueError]) -> None:
        problems: list[str] = []
        gone = self.deprecated.get(spec.model)
        if gone:
            hint = f"; use {gone.replacement}" if gone.replacement else ""
            problems.append(f"{spec.model} is deprecated (shutdown {gone.shutdown}){hint}")
        caps = self.capabilities_for(spec.model)
        problems += caps.problems(spec) if caps else []
        if problems:
            raise error(f"role {role!r} ({spec.label}): " + "; ".join(problems))

    def _resolved_default(self, settings: Settings) -> ModelSpec:
        override = settings.model.get(DEFAULT_ROLE)
        if override:
            return self.default.model_copy(update=parse_model_ref(override, "MODEL__DEFAULT"))
        provider = settings.llm_provider or self.default.provider
        env_model = settings.openai_model if provider == "openai" else settings.anthropic_model
        if env_model:
            return self.default.model_copy(update={"provider": provider, "model": env_model})
        if provider != self.default.provider:
            env_name = "OPENAI_MODEL" if provider == "openai" else "ANTHROPIC_MODEL"
            raise ConfigurationError(
                f"LLM_PROVIDER={provider} differs from models.yaml default; set {env_name} too"
            )
        return self.default


def parse_model_ref(ref: str, source: str) -> dict[str, Any]:
    """Parse `provider:model-id` into `{"provider": ..., "model": ...}`."""
    provider, sep, model = ref.partition(":")
    provider = provider.strip().lower()
    if not sep or not model.strip() or provider not in ("openai", "anthropic"):
        raise ConfigurationError(
            f"{source}={ref!r} must look like 'openai:<model-id>' or 'anthropic:<model-id>'"
        )
    return {"provider": provider, "model": model.strip()}


def load_models_config(path: Path | None = None) -> ModelsConfig:
    """Read a routing profile (the packaged default unless given) and validate it.

    The shared `model_catalog.yaml` (capabilities, pricing) and
    `deprecated_models.yaml` are merged in; entries the profile defines itself win.
    """
    path = path or DEFAULT_MODELS_PATH
    try:
        profile = _read_yaml(path)
        catalog = _read_yaml(CATALOG_PATH)
        merged = {
            **profile,
            "capabilities": {**catalog.get("capabilities", {}), **profile.get("capabilities", {})},
            "pricing": {**catalog.get("pricing", {}), **profile.get("pricing", {})},
            "deprecated": _read_yaml(DEPRECATIONS_PATH).get("deprecated", {}),
        }
        return ModelsConfig.model_validate(merged)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Models config not found: {exc.filename}") from exc
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid models config {path}: {exc}") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise yaml.YAMLError(f"{path.name} must contain a mapping at the top level")
    return data

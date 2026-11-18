"""Route each role to its configured provider and model.

Agents call `router.for_role("planner")` and never choose a model themselves. One
router serves a whole run, so providers can be mixed and usage is shared.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ticketideai.config.models import (
    ConfigurationError,
    ModelsConfig,
    ModelSpec,
    load_models_config,
)
from ticketideai.config.settings import ProviderName, Settings
from ticketideai.llm.anthropic_provider import AnthropicProvider
from ticketideai.llm.base import LLMProvider
from ticketideai.llm.openai_provider import OpenAIProvider
from ticketideai.llm.pricing import RunUsage
from ticketideai.observability.tracing import Tracer

logger = logging.getLogger(__name__)

ClientFactory = Callable[[ProviderName], Any]

_PROVIDER_CLASSES: dict[ProviderName, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}


class ModelRouter:
    """Hand out role-bound `LLMProvider` instances that share tracing and usage."""

    def __init__(
        self,
        settings: Settings,
        models: ModelsConfig,
        *,
        tracer: Tracer | None = None,
        usage: RunUsage | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.models = models
        self.tracer = tracer or Tracer()
        self.usage = usage or RunUsage()
        self._client_factory = client_factory or self._sdk_client
        self._clients: dict[ProviderName, Any] = {}
        self._providers: dict[str, LLMProvider] = {}

    @classmethod
    def from_settings(cls, settings: Settings, *, tracer: Tracer | None = None) -> ModelRouter:
        """Build a router from settings, loading `models.yaml` (or `MODELS_CONFIG_PATH`)."""
        return cls(settings, load_models_config(settings.models_config_path), tracer=tracer)

    def resolve(self, role: str) -> ModelSpec:
        """Return the model spec a role will use, after overrides."""
        return self.models.resolve(role, self.settings)

    def for_role(self, role: str) -> LLMProvider:
        """Return the provider for `role`, creating it on first use.

        Raises:
            ConfigurationError: If the role's provider has no API key configured.
        """
        role = role.lower()
        if role not in self._providers:
            spec = self.resolve(role)
            self._providers[role] = _PROVIDER_CLASSES[spec.provider](
                spec,
                client=self._client(spec.provider),
                role=role,
                tracer=self.tracer,
                usage=self.usage,
                pricing=self.models.price_for(spec.model),
            )
            logger.info(
                "model routed",
                extra={
                    "role": role,
                    "model": spec.label,
                    "validated": self.models.capabilities_for(spec.model) is not None,
                },
            )
        return self._providers[role]

    def _client(self, provider: ProviderName) -> Any:
        if provider not in self._clients:
            self._clients[provider] = self._client_factory(provider)
        return self._clients[provider]

    def _sdk_client(self, provider: ProviderName) -> Any:
        key = self.settings.api_key(provider)
        if not key:
            env_name = f"{provider.upper()}_API_KEY"
            raise ConfigurationError(f"{env_name} is not set; add it to your environment or .env")
        timeout, retries = self.settings.llm_timeout_seconds, self.settings.llm_max_retries
        if provider == "openai":
            import openai

            return openai.OpenAI(api_key=key, timeout=timeout, max_retries=retries)
        import anthropic

        return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=retries)

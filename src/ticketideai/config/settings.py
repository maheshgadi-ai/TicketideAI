"""Application settings loaded from environment variables and `.env`."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["openai", "anthropic"]


class Settings(BaseSettings):
    """Runtime configuration: credentials, transport behavior, and overrides.

    Which model each role uses lives in `config/models.yaml`. The fields here only
    override it:

    - `MODEL__<ROLE>=provider:model-id` overrides one role (e.g. `MODEL__PLANNER`).
    - `LLM_PROVIDER` plus `OPENAI_MODEL` / `ANTHROPIC_MODEL` override the `default` entry.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Credentials
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # Model routing overrides (all optional)
    models_config_path: Path | None = None
    llm_provider: ProviderName | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None
    model: dict[str, str] = Field(default_factory=dict)

    # Transport behavior shared by all providers
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # Store
    database_url: str = "data/ticketide.sqlite"  # SQLite path or sqlite:/// URL
    store_as_of: datetime | None = None  # fixed "today" for the seeded demo store

    # Reflection: corrected plans allowed after a failed plan
    max_reflections: int = Field(default=2, ge=0, le=2)

    # Observability
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # Logging
    log_level: str = "INFO"

    @field_validator("store_as_of", mode="before")
    @classmethod
    def _blank_date_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def tracing_enabled(self) -> bool:
        """Tracing is on only when both Langfuse keys are present and non-empty."""
        return bool(
            self.langfuse_public_key
            and self.langfuse_public_key.get_secret_value()
            and self.langfuse_secret_key
            and self.langfuse_secret_key.get_secret_value()
        )

    def api_key(self, provider: ProviderName) -> str | None:
        """Return the API key for `provider`, or None if it isn't set."""
        secret = self.openai_api_key if provider == "openai" else self.anthropic_api_key
        return (secret.get_secret_value() or None) if secret else None

    def now(self) -> datetime:
        """Return the store's current time: `STORE_AS_OF` when set, else the real clock."""
        return self.store_as_of or datetime.now()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()

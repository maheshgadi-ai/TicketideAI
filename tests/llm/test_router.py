import pytest

from tests.fakes import (
    FakeLangfuse,
    anthropic_response,
    fake_anthropic_client,
    fake_openai_client,
    openai_response,
)
from ticketideai.config import ConfigurationError, ModelsConfig, Settings
from ticketideai.llm import Message, ModelRouter
from ticketideai.llm.anthropic_provider import AnthropicProvider
from ticketideai.llm.openai_provider import OpenAIProvider
from ticketideai.observability import Tracer

MODELS = ModelsConfig.model_validate(
    {
        "default": {"provider": "openai", "model": "gpt-default"},
        "roles": {
            "responder": {"provider": "anthropic", "model": "claude-responder"},
            "judge": {"provider": "openai", "model": "gpt-judge"},
        },
        "pricing": {"claude-responder": {"input_per_mtok": 10, "output_per_mtok": 20}},
    }
)


class ClientFactory:
    """Hands out one fake client per provider and counts how often it's asked."""

    def __init__(self, openai_responses=(), anthropic_responses=()) -> None:
        self.clients = {
            "openai": fake_openai_client(*openai_responses),
            "anthropic": fake_anthropic_client(*anthropic_responses),
        }
        self.requests: list[str] = []

    def __call__(self, provider: str):
        self.requests.append(provider)
        return self.clients[provider]


def make_router(factory: ClientFactory, **kwargs) -> ModelRouter:
    return ModelRouter(Settings(), MODELS, client_factory=factory, **kwargs)


def test_routes_roles_to_mixed_providers() -> None:
    router = make_router(ClientFactory())
    responder, judge, planner = (router.for_role(r) for r in ("responder", "judge", "planner"))

    assert isinstance(responder, AnthropicProvider)
    assert responder.model == "claude-responder"
    assert isinstance(judge, OpenAIProvider)
    assert judge.model == "gpt-judge"
    assert planner.model == "gpt-default"  # fallback
    assert (responder.role, judge.role, planner.role) == (
        "responder",
        "judge",
        "planner",
    )


def test_providers_and_clients_are_cached() -> None:
    factory = ClientFactory()
    router = make_router(factory)
    assert router.for_role("judge") is router.for_role("JUDGE")
    router.for_role("planner")
    router.for_role("responder")
    assert sorted(factory.requests) == ["anthropic", "openai"]  # one client per provider


def test_env_override_switches_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL__JUDGE", "anthropic:claude-judge")
    router = make_router(ClientFactory())
    judge = router.for_role("judge")
    assert isinstance(judge, AnthropicProvider)
    assert judge.model == "claude-judge"


def test_mixed_run_shares_usage_and_trace_tags_each_call() -> None:
    langfuse = FakeLangfuse()
    factory = ClientFactory(
        openai_responses=[openai_response("critique", input_tokens=7, output_tokens=3)],
        anthropic_responses=[anthropic_response("draft", input_tokens=100, output_tokens=50)],
    )
    router = make_router(factory, tracer=Tracer(langfuse))

    router.for_role("responder").complete([Message.user("write")])
    router.for_role("judge").complete([Message.user("review")])

    usage = router.usage
    assert usage.calls == 2
    assert usage.by_role["responder"].total_tokens == 150
    assert usage.by_role["judge"].total_tokens == 10
    assert usage.cost_usd == pytest.approx(100 * 10 / 1e6 + 50 * 20 / 1e6)
    tags = [
        (o.start_kwargs["metadata"]["role"], o.start_kwargs["metadata"]["provider"])
        for o in langfuse.observations
    ]
    assert tags == [("responder", "anthropic"), ("judge", "openai")]


def test_missing_key_for_used_provider_raises() -> None:
    router = ModelRouter(Settings(), MODELS)
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        router.for_role("responder")


def test_key_only_needed_for_providers_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    router = ModelRouter(Settings(), MODELS)
    assert isinstance(router.for_role("judge"), OpenAIProvider)  # real SDK client, no call made


def test_from_settings_uses_custom_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text("default: {provider: anthropic, model: custom}\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(path))
    router = ModelRouter.from_settings(Settings())
    assert router.resolve("anything").label == "anthropic:custom"

import pytest

from tests.fakes import FakeLangfuse, fake_openai_client, openai_response
from ticketideai.config import ModelSpec, Pricing, Settings
from ticketideai.llm import Message
from ticketideai.llm.openai_provider import OpenAIProvider
from ticketideai.observability import Tracer, build_tracer


def test_disabled_tracer_is_noop() -> None:
    tracer = Tracer()
    with tracer.observe("step", input={"a": 1}) as obs:
        obs.update(output="x")
    tracer.score("relevance", 4)
    tracer.flush()
    assert not tracer.enabled
    assert tracer.current_trace_id() is None


def test_build_tracer_without_keys_is_disabled() -> None:
    assert not build_tracer(Settings()).enabled


def test_build_tracer_with_keys_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    seen: dict = {}

    def fake_langfuse(**kwargs):
        seen.update(kwargs)
        return FakeLangfuse()

    monkeypatch.setattr("langfuse.Langfuse", fake_langfuse)
    tracer = build_tracer(Settings())
    assert tracer.enabled
    assert seen["public_key"] == "pk-test"
    assert seen["base_url"] == "https://cloud.langfuse.com"


def test_observe_records_and_marks_errors() -> None:
    client = FakeLangfuse()
    tracer = Tracer(client)

    with tracer.observe("agent", as_type="agent", input="q") as obs:
        obs.update(output="plan")
    with pytest.raises(ValueError), tracer.observe("responder"):
        raise ValueError("bad step")

    agent, responder = client.observations
    assert agent.start_kwargs == {"name": "agent", "as_type": "agent", "input": "q"}
    assert agent.updates == [{"output": "plan"}]
    assert responder.updates[0]["level"] == "ERROR"


def test_scores_and_flush() -> None:
    client = FakeLangfuse()
    tracer = Tracer(client)
    tracer.score("groundedness", 5)
    tracer.score("relevance", 4, trace_id="abc", comment="good")
    tracer.flush()

    assert client.scores[0] == {"name": "groundedness", "value": 5, "comment": None}
    assert client.scores[1]["trace_id"] == "abc"
    assert client.flushed


def test_broken_client_never_breaks_app() -> None:
    class Broken:
        def start_as_current_observation(self, **_):
            raise RuntimeError("langfuse down")

        def score_current_trace(self, **_):
            raise RuntimeError("langfuse down")

        def flush(self):
            raise RuntimeError("langfuse down")

    tracer = Tracer(Broken())
    with tracer.observe("step") as obs:
        obs.update(output="still fine")
    tracer.score("x", 1)
    tracer.flush()


SPEC = ModelSpec(provider="openai", model="test-model")


def test_llm_call_emits_generation_with_role_usage_and_cost() -> None:
    client = FakeLangfuse()
    provider = OpenAIProvider(
        SPEC,
        client=fake_openai_client(openai_response("ok", input_tokens=100, output_tokens=50)),
        role="responder",
        tracer=Tracer(client),
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=2.0),
    )
    provider.complete([Message.user("hi")], trace_name="responder.draft")

    (gen,) = client.observations
    assert gen.start_kwargs["name"] == "responder.draft"
    assert gen.start_kwargs["as_type"] == "generation"
    assert gen.start_kwargs["model"] == "test-model"
    meta = gen.start_kwargs["metadata"]
    assert (meta["role"], meta["provider"], meta["model"]) == ("responder", "openai", "test-model")
    update = gen.updates[0]
    assert update["usage_details"] == {"input": 100, "output": 50}
    assert update["cost_details"]["total"] == pytest.approx(0.0002)


def test_llm_call_omits_cost_when_pricing_unknown() -> None:
    client = FakeLangfuse()
    provider = OpenAIProvider(
        SPEC, client=fake_openai_client(openai_response()), tracer=Tracer(client)
    )
    provider.complete([Message.user("hi")])
    assert "cost_details" not in client.observations[0].updates[0]


class RecordingPropagate:
    """Stands in for langfuse.propagate_attributes and records call order."""

    def __init__(self, events: list) -> None:
        self.events = events

    def __call__(self, **attributes):
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            self.events.append(("enter", attributes))
            yield
            self.events.append(("exit", None))

        return ctx()


def test_every_observation_carries_app_tag_and_metadata() -> None:
    events: list = []
    client = FakeLangfuse()
    original = client.start_as_current_observation

    def start(**kwargs):
        events.append(("start", kwargs["name"]))
        return original(**kwargs)

    client.start_as_current_observation = start
    tracer = Tracer(client, propagate=RecordingPropagate(events))
    with tracer.observe("generate_sql", as_type="chain"), tracer.observe("sql.validate"):
        pass

    attrs = {"tags": ["app:ticketideai"], "metadata": {"app": "ticketideai"}}
    assert events[:4] == [
        ("enter", attrs),
        ("start", "generate_sql"),
        ("enter", attrs),
        ("start", "sql.validate"),
    ]
    assert [e[0] for e in events].count("exit") == 2


def test_broken_propagate_never_breaks_tracing() -> None:
    def broken(**_):
        raise RuntimeError("no otel context")

    client = FakeLangfuse()
    with Tracer(client, propagate=broken).observe("step") as obs:
        obs.update(output="fine")
    assert client.observations[0].updates == [{"output": "fine"}]


def test_build_tracer_wires_propagate_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    import langfuse

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr("langfuse.Langfuse", lambda **_: FakeLangfuse())
    tracer = build_tracer(Settings())
    assert tracer._propagate is langfuse.propagate_attributes

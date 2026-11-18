"""What actually leaves the process for Langfuse, using the real SDKs with no network."""

import json

import anthropic
import httpx2
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ticketideai.config import ModelSpec, Pricing, Settings
from ticketideai.llm import Message
from ticketideai.llm.anthropic_provider import AnthropicProvider
from ticketideai.observability import build_tracer

ANTHROPIC_SCOPE = "com.anthropic.sdk.python"
USAGE_ATTRIBUTE = "langfuse.observation.usage_details"
COST_ATTRIBUTE = "langfuse.observation.cost_details"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 100, "output_tokens": 50},
}


def _anthropic_client(provider: TracerProvider) -> anthropic.Anthropic:
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(200, content=json.dumps(MESSAGE))
    )
    return anthropic.Anthropic(
        api_key="test-key",
        http_client=httpx2.Client(transport=transport),
        open_telemetry={"tracer_provider": provider},
    )


def test_each_model_call_reports_usage_and_cost_on_one_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langfuse import Langfuse

    exported, emitted = InMemorySpanExporter(), InMemorySpanExporter()
    otel = TracerProvider()
    otel.add_span_processor(SimpleSpanProcessor(emitted))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-export-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-export-test")
    monkeypatch.setattr(
        "langfuse.Langfuse",
        lambda **kwargs: Langfuse(**kwargs, tracer_provider=otel, span_exporter=exported),
    )
    tracer = build_tracer(Settings())
    provider = AnthropicProvider(
        ModelSpec(provider="anthropic", model="test-model"),
        client=_anthropic_client(otel),
        tracer=tracer,
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=2.0),
    )

    provider.complete([Message(role="user", content="hi")], trace_name="agent.planner.structured")
    tracer.flush()
    tracer._client.shutdown()

    scopes = {span.instrumentation_scope.name for span in emitted.get_finished_spans()}
    assert ANTHROPIC_SCOPE in scopes, "the SDK must emit its own span for this test to prove much"
    spans = exported.get_finished_spans()
    assert [span.name for span in spans] == ["agent.planner.structured"]
    assert json.loads(spans[0].attributes[USAGE_ATTRIBUTE]) == {"input": 100, "output": 50}
    assert json.loads(spans[0].attributes[COST_ATTRIBUTE])["total"] == pytest.approx(0.0002)

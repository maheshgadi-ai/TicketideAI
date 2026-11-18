import json

import openai
import pytest
from pydantic import BaseModel

from tests.fakes import fake_openai_client, openai_response
from ticketideai.config import ModelSpec, Pricing
from ticketideai.llm import (
    LLMError,
    Message,
    ProviderState,
    RunUsage,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
)
from ticketideai.llm.openai_provider import (
    OpenAIProvider,
    to_openai_input,
    to_openai_tool_choice,
)

SEARCH = ToolSpec(
    name="search",
    description="Search the web",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
)


class Plan(BaseModel):
    steps: list[str]


def make(*responses, spec: dict | None = None, **kwargs) -> tuple[OpenAIProvider, list[dict]]:
    client = fake_openai_client(*responses)
    model_spec = ModelSpec(provider="openai", model="test-model", **(spec or {}))
    provider = OpenAIProvider(model_spec, client=client, **kwargs)
    return provider, client.responses.create.calls


# --- Request building ---------------------------------------------------------


def test_input_conversion_without_provider_state() -> None:
    msgs = [
        Message.system("be brief"),
        Message.system("cite sources"),
        Message.user("find x"),
        Message.assistant("Looking.", [ToolCall(id="c1", name="search", arguments={"q": "x"})]),
        Message.tool("c1", "result"),
    ]
    instructions, items = to_openai_input(msgs)

    assert instructions == "be brief\n\ncite sources"
    assert items[0] == {"role": "user", "content": "find x"}
    assert items[1] == {"role": "assistant", "content": "Looking."}
    assert items[2]["type"] == "function_call"
    assert items[2]["call_id"] == "c1"
    assert json.loads(items[2]["arguments"]) == {"q": "x"}
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "result"}


def test_openai_provider_state_is_replayed_verbatim() -> None:
    replay = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
        {"type": "function_call", "call_id": "c1", "name": "search", "arguments": "{}"},
    ]
    msg = Message.assistant(
        tool_calls=[ToolCall(id="c1", name="search")],
        provider_state=ProviderState(provider="openai", items=replay),
    )
    _, items = to_openai_input([Message.user("q"), msg, Message.tool("c1", "r")])
    assert items[1:3] == replay
    assert items[3]["type"] == "function_call_output"


def test_other_providers_state_is_ignored() -> None:
    msg = Message.assistant(
        "hi", provider_state=ProviderState(provider="anthropic", items=[{"type": "thinking"}])
    )
    _, items = to_openai_input([msg])
    assert items == [{"role": "assistant", "content": "hi"}]


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("auto", "auto"),
        ("required", "required"),
        ("none", "none"),
        ("search", {"type": "function", "name": "search"}),
    ],
)
def test_tool_choice_mapping(choice, expected) -> None:
    assert to_openai_tool_choice(choice) == expected


def test_minimal_request_shape() -> None:
    provider, calls = make(openai_response("hello"))
    provider.complete([Message.system("sys"), Message.user("hi")], max_tokens=50)

    body = calls[0]
    assert body["model"] == "test-model"
    assert body["instructions"] == "sys"
    assert body["input"] == [{"role": "user", "content": "hi"}]
    assert body["max_output_tokens"] == 50
    assert body["store"] is False
    for absent in ("reasoning", "temperature", "tools", "tool_choice"):
        assert absent not in body


def test_reasoning_effort_and_temperature_sent_when_configured() -> None:
    provider, calls = make(openai_response(), spec={"reasoning_effort": "none", "temperature": 0.3})
    provider.complete([Message.user("hi")])
    assert calls[0]["reasoning"] == {"effort": "none"}
    assert calls[0]["temperature"] == 0.3


def test_tools_use_responses_function_shape() -> None:
    provider, calls = make(openai_response(None, tool_calls=[("c1", "search", '{"q": "llm"}')]))
    provider.complete([Message.user("go")], tools=[SEARCH], tool_choice="auto")

    assert calls[0]["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "Search the web",
            "parameters": SEARCH.parameters,
            "strict": False,
        }
    ]
    assert calls[0]["tool_choice"] == "auto"


# --- Response parsing ---------------------------------------------------------


def test_text_response_and_usage() -> None:
    usage = RunUsage()
    provider, _ = make(
        openai_response("hello", input_tokens=1000, output_tokens=500, reasoning_tokens=200),
        usage=usage,
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=8.0),
    )
    resp = provider.complete([Message.user("hi")])

    assert resp.content == "hello"
    assert resp.stop_reason == "completed"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (1000, 500)
    assert resp.usage.reasoning_tokens == 200
    assert usage.cost_usd == pytest.approx(1000 * 2 / 1e6 + 500 * 8 / 1e6)


def test_tool_calls_parsed_and_state_captured_for_replay() -> None:
    provider, _ = make(
        openai_response(None, tool_calls=[("c1", "search", '{"q": "llm"}')], reasoning="enc")
    )
    resp = provider.complete([Message.user("go")], tools=[SEARCH])

    assert resp.stop_reason == "tool_calls"
    assert resp.tool_calls == [ToolCall(id="c1", name="search", arguments={"q": "llm"})]
    assert resp.provider_state is not None
    types = [i["type"] for i in resp.provider_state.items]
    assert types == ["reasoning", "function_call"]
    assert resp.provider_state.items[0]["encrypted_content"] == "enc"


def test_tool_loop_round_trip_replays_reasoning() -> None:
    provider, calls = make(
        openai_response(None, tool_calls=[("c1", "search", '{"q": "x"}')], reasoning="enc"),
        openai_response("final answer"),
    )
    history = [Message.user("find x")]
    first = provider.complete(history, tools=[SEARCH])
    history += [first.to_message(), Message.tool("c1", "x is 42")]
    second = provider.complete(history, tools=[SEARCH])

    second_input = calls[1]["input"]
    assert [i.get("type") for i in second_input[1:]] == [
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert second.content == "final answer"


def test_refusal_reported() -> None:
    provider, _ = make(openai_response(None, refusal="I can't help with that."))
    resp = provider.complete([Message.user("bad")])
    assert resp.stop_reason == "refusal"
    assert resp.content == "I can't help with that."


def test_incomplete_reports_reason() -> None:
    provider, _ = make(
        openai_response(None, status="incomplete", incomplete_reason="max_output_tokens")
    )
    assert provider.complete([Message.user("hi")]).stop_reason == "max_output_tokens"


def test_malformed_tool_arguments_raise() -> None:
    provider, _ = make(openai_response(None, tool_calls=[("c1", "search", "{not json")]))
    with pytest.raises(LLMError):
        provider.complete([Message.user("go")], tools=[SEARCH])


def test_sdk_error_wrapped() -> None:
    provider, _ = make(openai.OpenAIError("boom"))
    with pytest.raises(LLMError, match="boom"):
        provider.complete([Message.user("hi")])


def test_role_usage_breakdown() -> None:
    usage = RunUsage()
    provider, _ = make(openai_response(), usage=usage, role="judge")
    provider.complete([Message.user("hi")])
    assert usage.by_role["judge"].calls == 1
    assert usage.by_role["judge"].total_tokens == 15


# --- structured() -------------------------------------------------------------


def test_structured_output_forces_function_and_validates() -> None:
    provider, calls = make(
        openai_response(
            None, tool_calls=[("c1", "submit_plan", '{"steps": ["a", "b"]}')], reasoning="enc"
        ),
        spec={"reasoning_effort": "high"},
    )
    plan = provider.structured([Message.user("plan it")], Plan)

    assert plan == Plan(steps=["a", "b"])
    assert calls[0]["tool_choice"] == {"type": "function", "name": "submit_plan"}
    assert calls[0]["tools"][0]["parameters"]["properties"]["steps"]
    assert calls[0]["reasoning"] == {"effort": "high"}


def test_structured_output_invalid_raises() -> None:
    provider, _ = make(openai_response(None, tool_calls=[("c1", "submit_plan", '{"steps": 3}')]))
    with pytest.raises(StructuredOutputError):
        provider.structured([Message.user("plan it")], Plan)


def test_structured_output_truncated_explains_why() -> None:
    provider, _ = make(
        openai_response(None, status="incomplete", incomplete_reason="max_output_tokens")
    )
    with pytest.raises(StructuredOutputError, match="max_output_tokens"):
        provider.structured([Message.user("plan it")], Plan)

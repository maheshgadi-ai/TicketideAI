import anthropic
import pytest
from pydantic import BaseModel, Field

from tests.fakes import anthropic_response, fake_anthropic_client
from ticketideai.config import ModelSpec
from ticketideai.llm import (
    LLMError,
    Message,
    ProviderState,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
)
from ticketideai.llm.anthropic_provider import (
    AnthropicProvider,
    to_anthropic_messages,
    to_anthropic_tool_choice,
)

SEARCH = ToolSpec(
    name="search",
    description="Search the web",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}},
)


class Verdict(BaseModel):
    passed: bool
    notes: str


def make(*responses, spec: dict | None = None, **kwargs) -> tuple[AnthropicProvider, list[dict]]:
    client = fake_anthropic_client(*responses)
    model_spec = ModelSpec(provider="anthropic", model="test-model", **(spec or {}))
    return AnthropicProvider(model_spec, client=client, **kwargs), client.messages.create.calls


def test_system_is_split_out_and_tool_results_merged() -> None:
    msgs = [
        Message.system("rule one"),
        Message.system("rule two"),
        Message.user("find x and y"),
        Message.assistant(
            "Searching.",
            tool_calls=[
                ToolCall(id="t1", name="search", arguments={"q": "x"}),
                ToolCall(id="t2", name="search", arguments={"q": "y"}),
            ],
        ),
        Message.tool("t1", "x-result"),
        Message.tool("t2", "y-result"),
    ]
    system, out = to_anthropic_messages(msgs)

    assert system == "rule one\n\nrule two"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][0] == {"type": "text", "text": "Searching."}
    assert out[1]["content"][1]["type"] == "tool_use"
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["t1", "t2"]


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        ("none", {"type": "none"}),
        ("search", {"type": "tool", "name": "search"}),
    ],
)
def test_tool_choice_mapping(choice, expected) -> None:
    assert to_anthropic_tool_choice(choice) == expected


def test_complete_builds_request() -> None:
    provider, calls = make(anthropic_response("hello"), spec={"max_tokens": 256})
    resp = provider.complete([Message.system("sys"), Message.user("hi")])

    assert resp.content == "hello"
    assert resp.usage.total_tokens == 15
    assert calls[0]["system"] == "sys"
    assert calls[0]["max_tokens"] == 256
    assert "temperature" not in calls[0]
    assert "output_config" not in calls[0]
    assert provider.usage.calls == 1


def test_complete_parses_text_and_tool_use() -> None:
    provider, calls = make(
        anthropic_response("Let me look.", tool_uses=[("t1", "search", {"q": "llm"})])
    )
    resp = provider.complete([Message.user("go")], tools=[SEARCH], tool_choice="required")

    assert calls[0]["tools"][0]["input_schema"] == SEARCH.parameters
    assert calls[0]["tool_choice"] == {"type": "any"}
    assert resp.content == "Let me look."
    assert resp.tool_calls[0].arguments == {"q": "llm"}


def test_reasoning_effort_maps_to_output_config() -> None:
    provider, calls = make(anthropic_response(), spec={"reasoning_effort": "high"})
    provider.complete([Message.user("hi")])
    assert calls[0]["output_config"] == {"effort": "high"}


def test_sdk_error_wrapped() -> None:
    provider, _ = make(anthropic.AnthropicError("down"))
    with pytest.raises(LLMError, match="down"):
        provider.complete([Message.user("hi")])


def test_structured_uses_output_format_not_forced_tool() -> None:
    provider, calls = make(
        anthropic_response('{"passed": true, "notes": "ok"}', thinking_signature="sig"),
        spec={"reasoning_effort": "medium"},
    )
    verdict = provider.structured([Message.user("judge")], Verdict)

    assert verdict == Verdict(passed=True, notes="ok")
    body = calls[0]
    assert "tool_choice" not in body
    assert "tools" not in body
    fmt = body["output_config"]["format"]
    assert body["output_config"]["effort"] == "medium"
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == {"passed", "notes"}


def test_structured_schema_moves_unsupported_constraints_but_still_validates() -> None:
    class Score(BaseModel):
        value: int = Field(ge=1, le=5)

    provider, calls = make(anthropic_response('{"value": 9}'))
    with pytest.raises(StructuredOutputError, match="Invalid Score"):
        provider.structured([Message.user("score")], Score)
    sent = calls[0]["output_config"]["format"]["schema"]["properties"]["value"]
    assert "minimum" not in sent  # unsupported by structured outputs; moved to description


@pytest.mark.parametrize(
    ("text", "stop_reason", "message"),
    [
        (None, "max_tokens", "stop_reason='max_tokens'"),
        ("I can't help with that.", "refusal", "stop_reason='refusal'"),
        ("not json", "end_turn", "Invalid Verdict"),
    ],
)
def test_structured_failures_explain_why(text, stop_reason, message) -> None:
    provider, _ = make(anthropic_response(text, stop_reason=stop_reason))
    with pytest.raises(StructuredOutputError, match=message):
        provider.structured([Message.user("judge")], Verdict)


def test_thinking_blocks_replayed_unchanged_in_tool_loop() -> None:
    provider, calls = make(
        anthropic_response(
            None, tool_uses=[("t1", "search", {"q": "x"})], thinking_signature="sig"
        ),
        anthropic_response("done"),
    )
    history = [Message.user("find x")]
    first = provider.complete(history, tools=[SEARCH], tool_choice="auto")
    history += [first.to_message(), Message.tool("t1", "x is 42")]
    provider.complete(history, tools=[SEARCH], tool_choice="auto")

    assistant_turn = calls[1]["messages"][1]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert assistant_turn["content"][1]["type"] == "tool_use"
    assert calls[1]["messages"][2]["content"][0]["type"] == "tool_result"


def test_openai_state_ignored_by_anthropic() -> None:
    msg = Message.assistant(
        "hi", provider_state=ProviderState(provider="openai", items=[{"type": "reasoning"}])
    )
    _, out = to_anthropic_messages([msg])
    assert out == [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]

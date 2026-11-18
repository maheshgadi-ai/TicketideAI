"""Test doubles for SDK clients and the Langfuse client."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace as NS
from typing import Any

from anthropic.types import Message as AnthropicMessage
from openai.types.responses import Response


class RecordingCreate:
    """Callable that records kwargs and returns queued responses (or raises)."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def fake_openai_client(*responses: Any) -> NS:
    """Object shaped like `openai.OpenAI` with a recording `responses.create`."""
    return NS(responses=NS(create=RecordingCreate(*responses)))


def fake_anthropic_client(*responses: Any) -> NS:
    """Object shaped like `anthropic.Anthropic` with a recording `messages.create`."""
    return NS(messages=NS(create=RecordingCreate(*responses)))


def openai_response(
    content: str | None = "hi",
    tool_calls: list[tuple[str, str, str]] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
    reasoning_tokens: int = 0,
    reasoning: str | None = None,
    refusal: str | None = None,
    status: str = "completed",
    incomplete_reason: str | None = None,
) -> Response:
    """Build a real, validated Responses API `Response`.

    tool_calls: (call_id, name, json_args). `reasoning` adds a reasoning item with
    that encrypted_content first, as reasoning models return it.
    """
    output: list[dict[str, Any]] = []
    if reasoning is not None:
        output.append(
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": reasoning}
        )
    parts: list[dict[str, Any]] = []
    if content:
        parts.append({"type": "output_text", "text": content, "annotations": []})
    if refusal:
        parts.append({"type": "refusal", "refusal": refusal})
    if parts:
        output.append(
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": parts,
            }
        )
    output += [
        {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "call_id": call_id,
            "name": name,
            "arguments": args,
            "status": "completed",
        }
        for call_id, name, args in tool_calls or []
    ]
    return Response.model_validate(
        {
            "id": "resp_1",
            "created_at": 0,
            "model": "test-model",
            "object": "response",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": status,
            "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            },
        }
    )


def anthropic_response(
    text: str | None = "hi",
    tool_uses: list[tuple[str, str, dict[str, Any]]] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
    thinking_signature: str | None = None,
) -> AnthropicMessage:
    """Build a real, validated Anthropic `Message`.

    tool_uses: (id, name, input). `thinking_signature` adds a leading thinking block
    (with empty text, as the "omitted" display default returns it).
    """
    blocks: list[dict[str, Any]] = []
    if thinking_signature is not None:
        blocks.append({"type": "thinking", "thinking": "", "signature": thinking_signature})
    if text:
        blocks.append({"type": "text", "text": text})
    blocks += [
        {"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in tool_uses or []
    ]
    return AnthropicMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


class FakeObservation:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.start_kwargs = kwargs
        self.updates: list[dict[str, Any]] = []

    def update(self, **fields: Any) -> None:
        self.updates.append(fields)


class FakeLangfuse:
    """Records observations and scores like the Langfuse client would."""

    def __init__(self) -> None:
        self.observations: list[FakeObservation] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = False

    @contextmanager
    def start_as_current_observation(self, **kwargs: Any):
        obs = FakeObservation(kwargs)
        self.observations.append(obs)
        yield obs

    def get_current_trace_id(self) -> str:
        return "trace-123"

    def score_current_trace(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed = True

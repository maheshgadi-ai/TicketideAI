"""Anthropic Messages API implementation of `LLMProvider`.

Current Claude models think adaptively by default. Two consequences shape this file:
- Thinking blocks must be sent back unchanged in tool loops, so each response's
  raw content blocks are kept in `provider_state` and replayed on the next turn.
- Claude Opus 5.5 and Sonnet 5.5 reject forced tool use, so `structured()` uses
  native structured outputs (`output_config.format`) instead of a forced tool.
"""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import BaseModel

from ticketideai.llm.base import LLMProvider
from ticketideai.llm.types import (
    CompletionRequest,
    LLMError,
    LLMResponse,
    Message,
    ProviderState,
    ToolCall,
    ToolSpec,
    Usage,
)

PROVIDER = "anthropic"
_CHOICE_MAP = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}


class AnthropicProvider(LLMProvider):
    """Chat model backed by the Anthropic API."""

    name = PROVIDER
    structured_mode = "json"

    def json_schema(self, schema: type[BaseModel]) -> dict[str, Any]:
        """Adapt a pydantic schema to structured-output rules with the SDK's helper.

        It adds `additionalProperties: false` and moves unsupported constraints
        (e.g. `minimum`) into descriptions; pydantic still enforces them afterward.
        """
        return anthropic.transform_schema(schema)

    def _complete(self, request: CompletionRequest) -> LLMResponse:
        system, converted = to_anthropic_messages(request.messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": request.max_tokens,
        }
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        output_config = _output_config(request)
        if output_config:
            body["output_config"] = output_config
        if request.tools:
            body["tools"] = [to_anthropic_tool(t) for t in request.tools]
            if request.tool_choice:
                body["tool_choice"] = to_anthropic_tool_choice(request.tool_choice)
        try:
            raw = self._client.messages.create(**body)
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        return from_anthropic_response(raw)


def _output_config(request: CompletionRequest) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if request.reasoning_effort:
        config["effort"] = request.reasoning_effort
    if request.response_format:
        config["format"] = {"type": "json_schema", "schema": request.response_format.json_schema}
    return config


def to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split out system text and convert the rest to Anthropic content blocks.

    Tool results become `tool_result` blocks in a user turn, and consecutive turns
    with the same role are merged, as the API requires alternating roles.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        role, blocks = _to_blocks(m)
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return "\n\n".join(p for p in system_parts if p), out


def _to_blocks(m: Message) -> tuple[str, list[dict[str, Any]]]:
    if m.role == "tool":
        block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
        return "user", [block]
    replay = m.state_for(PROVIDER)
    if replay is not None:
        return m.role, list(replay)  # unchanged, including thinking blocks and signatures
    blocks: list[dict[str, Any]] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for c in m.tool_calls:
        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
    return m.role, blocks


def to_anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    """Convert a neutral tool spec to an Anthropic tool definition."""
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def to_anthropic_tool_choice(choice: str) -> dict[str, Any]:
    """Map "auto"/"required"/"none", or a tool name to force that tool."""
    return _CHOICE_MAP.get(choice, {"type": "tool", "name": choice})


def from_anthropic_response(raw: Any) -> LLMResponse:
    """Normalize an Anthropic message object."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in raw.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
    usage = Usage(
        input_tokens=getattr(raw.usage, "input_tokens", 0) or 0,
        output_tokens=getattr(raw.usage, "output_tokens", 0) or 0,
    )
    return LLMResponse(
        content="".join(texts),
        tool_calls=calls,
        usage=usage,
        model=raw.model,
        stop_reason=raw.stop_reason,
        provider_state=ProviderState(
            provider=PROVIDER,
            items=[block.model_dump(mode="json", exclude_none=True) for block in raw.content],
        ),
    )

"""OpenAI Responses API implementation of `LLMProvider`.

Requests are stateless (`store=False`). The full output of each turn, including
reasoning items and their `encrypted_content`, is carried in
`Message.provider_state` and replayed on the next request, as the Responses API
requires when tool calls are involved.
"""

from __future__ import annotations

import json
from typing import Any

import openai

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

PROVIDER = "openai"
_CHOICE_KEYWORDS = {"auto", "required", "none"}


class OpenAIProvider(LLMProvider):
    """Chat model backed by the OpenAI Responses API."""

    name = PROVIDER

    def _complete(self, request: CompletionRequest) -> LLMResponse:
        instructions, items = to_openai_input(request.messages)
        body: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "max_output_tokens": request.max_tokens,
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if request.reasoning_effort:
            body["reasoning"] = {"effort": request.reasoning_effort}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [to_openai_tool(t) for t in request.tools]
            if request.tool_choice:
                body["tool_choice"] = to_openai_tool_choice(request.tool_choice)
        if request.response_format:
            fmt = request.response_format
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": fmt.name,
                    "schema": fmt.json_schema,
                    "strict": False,
                }
            }
        try:
            raw = self._client.responses.create(**body)
        except openai.OpenAIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        return from_openai_response(raw)


def to_openai_input(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split out system text as `instructions` and convert the rest to input items."""
    system_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content}
            )
        elif m.role == "assistant":
            items.extend(_assistant_items(m))
        else:
            items.append({"role": "user", "content": m.content})
    return "\n\n".join(p for p in system_parts if p), items


def _assistant_items(m: Message) -> list[dict[str, Any]]:
    replay = m.state_for(PROVIDER)
    if replay is not None:
        return list(replay)  # original order, including reasoning items
    items: list[dict[str, Any]] = []
    if m.content:
        items.append({"role": "assistant", "content": m.content})
    for c in m.tool_calls:
        items.append(
            {
                "type": "function_call",
                "call_id": c.id,
                "name": c.name,
                "arguments": json.dumps(c.arguments),
            }
        )
    return items


def to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    """Convert a neutral tool spec to a Responses function tool.

    `strict` is set to False explicitly: pydantic schemas don't meet strict-mode rules
    (every field required, `additionalProperties: false`), and arguments are
    validated with pydantic afterward anyway.
    """
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
        "strict": False,
    }


def to_openai_tool_choice(choice: str) -> str | dict[str, Any]:
    """Pass "auto"/"required"/"none" through, and map a tool name to a forced call."""
    if choice in _CHOICE_KEYWORDS:
        return choice
    return {"type": "function", "name": choice}


def from_openai_response(raw: Any) -> LLMResponse:
    """Normalize a Responses API `Response` object."""
    if getattr(raw, "error", None):
        raise LLMError(f"OpenAI response failed: {raw.error}")
    texts: list[str] = []
    calls: list[ToolCall] = []
    refused = False
    for item in raw.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    texts.append(part.text)
                elif part.type == "refusal":
                    texts.append(part.refusal)
                    refused = True
        elif item.type == "function_call":
            calls.append(ToolCall(id=item.call_id, name=item.name, arguments=_args(item.arguments)))
    return LLMResponse(
        content="".join(texts),
        tool_calls=calls,
        usage=_usage(raw.usage),
        model=raw.model,
        stop_reason=_stop_reason(raw, refused, bool(calls)),
        provider_state=ProviderState(
            provider=PROVIDER,
            items=[item.model_dump(mode="json", exclude_none=True) for item in raw.output],
        ),
    )


def _stop_reason(raw: Any, refused: bool, has_calls: bool) -> str | None:
    if raw.status == "incomplete":
        details = getattr(raw, "incomplete_details", None)
        return getattr(details, "reason", None) or "incomplete"
    if refused:
        return "refusal"
    return "tool_calls" if has_calls else raw.status


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
    )


def _args(arguments: str | None) -> dict[str, Any]:
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model returned malformed tool arguments: {arguments[:200]}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("Model returned non-object tool arguments")
    return parsed

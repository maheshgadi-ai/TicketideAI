"""Provider-neutral data types for LLM requests and responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProviderState(BaseModel):
    """Opaque output items a provider needs replayed on the next turn.

    OpenAI's Responses API requires reasoning items that came with tool calls to be
    sent back alongside the tool outputs. Only the provider named here uses these
    items; any other provider ignores them and falls back to the neutral fields.
    """

    provider: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class Message(BaseModel):
    """One chat message in a provider-neutral format.

    - `assistant` messages may carry `tool_calls` and `provider_state`.
    - `tool` messages answer a call and must set `tool_call_id`.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    provider_state: ProviderState | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        """Build a system message."""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        """Build a user message."""
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls,
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        provider_state: ProviderState | None = None,
    ) -> Message:
        """Build an assistant message, optionally with tool calls."""
        return cls(
            role="assistant",
            content=content,
            tool_calls=tool_calls or [],
            provider_state=provider_state,
        )

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> Message:
        """Build a tool-result message."""
        return cls(role="tool", content=content, tool_call_id=tool_call_id)

    def state_for(self, provider: str) -> list[dict[str, Any]] | None:
        """Return replay items if they were produced by `provider`, else None."""
        if self.provider_state and self.provider_state.provider == provider:
            return self.provider_state.items
        return None


class ToolSpec(BaseModel):
    """A tool the model may call, described by a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


class Usage(BaseModel):
    """Token counts for one call. `output_tokens` includes any reasoning tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Sum of input and output tokens."""
        return self.input_tokens + self.output_tokens


class ResponseFormat(BaseModel):
    """Ask the model to reply with JSON matching `schema` (native structured outputs)."""

    name: str
    json_schema: dict[str, Any]


class CompletionRequest(BaseModel):
    """Everything a provider needs for one call, with defaults already applied."""

    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: str | None = None
    response_format: ResponseFormat | None = None
    max_tokens: int
    temperature: float | None = None
    reasoning_effort: str | None = None


class LLMResponse(BaseModel):
    """Normalized result of a completion call."""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str
    stop_reason: str | None = None
    provider_state: ProviderState | None = None

    def to_message(self) -> Message:
        """Turn this response into the assistant message to append to the history."""
        return Message.assistant(self.content, self.tool_calls, self.provider_state)


class LLMError(RuntimeError):
    """Raised when a provider call fails."""


class StructuredOutputError(LLMError):
    """Raised when the model's structured output is missing or fails validation."""

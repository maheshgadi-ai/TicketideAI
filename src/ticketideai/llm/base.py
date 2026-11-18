"""The provider-neutral LLM interface."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from ticketideai.config.models import ModelSpec, Pricing
from ticketideai.llm.pricing import RunUsage, cost_of
from ticketideai.llm.types import (
    CompletionRequest,
    LLMResponse,
    Message,
    ResponseFormat,
    StructuredOutputError,
    ToolSpec,
)
from ticketideai.observability.tracing import Tracer

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MAX_TOKENS = 4096


class LLMProvider(ABC):
    """A chat model bound to one role, with tool calling and structured output.

    Subclasses implement `_complete`. The public `complete` method adds tracing
    (tagged with role, provider, and model), usage and cost accounting, and logging.
    """

    name: str = "base"
    structured_mode: Literal["tool", "json"] = "tool"

    def __init__(
        self,
        spec: ModelSpec,
        *,
        client: Any,
        role: str = "default",
        tracer: Tracer | None = None,
        usage: RunUsage | None = None,
        pricing: Pricing | None = None,
    ) -> None:
        self.spec = spec
        self.role = role
        self._client = client
        self.tracer = tracer or Tracer()
        self.usage = usage or RunUsage()
        self.pricing = pricing or Pricing()

    @property
    def model(self) -> str:
        """The model ID this provider calls."""
        return self.spec.model

    @abstractmethod
    def _complete(self, request: CompletionRequest) -> LLMResponse:
        """Call the provider and return a normalized response."""

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
        response_format: ResponseFormat | None = None,
        max_tokens: int | None = None,
        trace_name: str = "llm.complete",
    ) -> LLMResponse:
        """Run one chat completion.

        Args:
            messages: Conversation so far.
            tools: Tools the model may call.
            tool_choice: "auto", "required", "none", or a tool name to force that tool.
            response_format: Ask for a JSON reply matching a schema.
            max_tokens: Override the role's output token limit for this call.
            trace_name: Name of the generation in the trace.

        Returns:
            The normalized model response.
        """
        request = CompletionRequest(
            messages=messages,
            tools=tools or [],
            tool_choice=tool_choice,
            response_format=response_format,
            max_tokens=max_tokens or self.spec.max_tokens or DEFAULT_MAX_TOKENS,
            temperature=self.spec.temperature,
            reasoning_effort=self.spec.reasoning_effort,
        )
        with self.tracer.observe(
            trace_name,
            as_type="generation",
            model=self.model,
            input=[m.model_dump(exclude_defaults=True) for m in messages],
            metadata=self._trace_metadata(request),
        ) as obs:
            started = time.perf_counter()
            response = self._complete(request)
            elapsed = time.perf_counter() - started
            obs.update(**self._record(request, response))

        logger.info(
            "llm call complete",
            extra={
                "role": self.role,
                "provider": self.name,
                "model": response.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
                "stop_reason": response.stop_reason,
                "tool_calls": len(response.tool_calls),
                "latency_s": round(elapsed, 3),
            },
        )
        return response

    def structured(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        max_tokens: int | None = None,
        trace_name: str | None = None,
    ) -> T:
        """Return a validated instance of `schema` produced by the model.

        The strategy is per provider (`structured_mode`):
        - "tool": force a single tool call whose arguments follow the schema.
        - "json": native structured outputs; the reply text is JSON for the schema.
        Either way, the result is validated with pydantic.

        Raises:
            StructuredOutputError: If no object comes back or it fails validation.
        """
        trace_name = trace_name or f"llm.structured.{schema.__name__}"
        if self.structured_mode == "json":
            fmt = ResponseFormat(name=_snake(schema.__name__), json_schema=self.json_schema(schema))
            response = self.complete(
                messages, response_format=fmt, max_tokens=max_tokens, trace_name=trace_name
            )
            payload: Any = response.content.strip() or None
            validate = schema.model_validate_json
        else:
            tool = ToolSpec(
                name=f"submit_{_snake(schema.__name__)}",
                description=f"Submit the final answer as a {schema.__name__} object.",
                parameters=self.json_schema(schema),
            )
            response = self.complete(
                messages,
                tools=[tool],
                tool_choice=tool.name,
                max_tokens=max_tokens,
                trace_name=trace_name,
            )
            call = next((c for c in response.tool_calls if c.name == tool.name), None)
            payload = call.arguments if call else None
            validate = schema.model_validate
        if payload is None or response.stop_reason == "refusal":
            raise StructuredOutputError(
                f"Model did not return a {schema.__name__} object "
                f"(stop_reason={response.stop_reason!r}, role={self.role!r})"
            )
        try:
            return validate(payload)
        except ValidationError as exc:
            raise StructuredOutputError(f"Invalid {schema.__name__} output: {exc}") from exc

    def json_schema(self, schema: type[BaseModel]) -> dict[str, Any]:
        """JSON Schema sent to the provider for `schema`. Providers may adapt it."""
        return schema.model_json_schema()

    def _trace_metadata(self, request: CompletionRequest) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.name,
            "model": self.model,
            "tools": [t.name for t in request.tools],
            "response_format": request.response_format.name if request.response_format else None,
        }

    def _record(self, request: CompletionRequest, response: LLMResponse) -> dict[str, Any]:
        """Add the call to run usage and return the trace update for it."""
        input_cost, output_cost = cost_of(response.usage, self.pricing)
        self.usage.record(response.usage, input_cost + output_cost, role=self.role)
        params = {
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "reasoning_effort": request.reasoning_effort,
            "tool_choice": request.tool_choice,
        }
        update: dict[str, Any] = {
            "output": response.model_dump(include={"content", "tool_calls", "stop_reason"}),
            "metadata": {"reasoning_tokens": response.usage.reasoning_tokens},
            "model_parameters": {k: v for k, v in params.items() if v is not None},
            "usage_details": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        }
        if self.pricing.known:
            update["cost_details"] = {
                "input": input_cost,
                "output": output_cost,
                "total": input_cost + output_cost,
            }
        return update


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

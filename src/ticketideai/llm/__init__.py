"""Provider-neutral LLM layer: OpenAI and Anthropic providers behind a role router."""

from ticketideai.llm.base import LLMProvider
from ticketideai.llm.pricing import RoleUsage, RunUsage
from ticketideai.llm.router import ModelRouter
from ticketideai.llm.types import (
    CompletionRequest,
    LLMError,
    LLMResponse,
    Message,
    ProviderState,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = [
    "CompletionRequest",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ModelRouter",
    "ProviderState",
    "RoleUsage",
    "RunUsage",
    "StructuredOutputError",
    "ToolCall",
    "ToolSpec",
    "Usage",
]

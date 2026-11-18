"""Base agent pattern for TicketideAI role-specific agents."""

from __future__ import annotations

from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

from ticketideai.llm import LLMProvider, Message
from ticketideai.observability.tracing import Tracer

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class BaseAgent(Generic[InputT, OutputT]):
    """Load a file prompt and run one structured LLM call with tracing."""

    name: str
    role: str

    def __init__(
        self,
        provider: LLMProvider,
        prompt_path: str | Path,
        tracer: Tracer | None = None,
    ) -> None:
        self.provider = provider
        self.prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.tracer = tracer or getattr(provider, "tracer", None) or Tracer()

    def run(self, input_data: InputT, output_schema: type[OutputT]) -> OutputT:
        with self.tracer.observe(
            f"agent.{self.name}",
            as_type="agent",
            input=input_data.model_dump(),
            metadata={"role": self.role},
            model=self.provider.model,
        ) as observation:
            payload = self.provider.structured(
                [
                    Message.system(self.prompt),
                    Message.user(input_data.model_dump_json()),
                ],
                output_schema,
                trace_name=f"agent.{self.name}.structured",
            )
            observation.update(output=payload.model_dump())
            return payload

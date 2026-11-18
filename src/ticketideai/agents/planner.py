"""Planner agent: writes and corrects `plan(api)` source for a support request."""

from __future__ import annotations

from pathlib import Path

from ticketideai.agents.base import BaseAgent
from ticketideai.llm import LLMProvider
from ticketideai.observability.tracing import Tracer
from ticketideai.schemas.store import PlanRequest, PlanResponse

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"


class PlannerAgent(BaseAgent[PlanRequest, PlanResponse]):
    """Generate a plan for a request, or a corrected plan after an error."""

    name = "planner"
    role = "planner"

    def __init__(self, provider: LLMProvider, tracer: Tracer | None = None) -> None:
        super().__init__(provider, PROMPT_PATH, tracer)

    def plan(self, request: PlanRequest) -> PlanResponse:
        """Return a summary and plan source for `request`."""
        return self.run(request, PlanResponse)

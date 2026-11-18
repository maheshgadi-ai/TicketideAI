"""Shared helpers for store, sandbox, and workflow tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ticketideai.orchestrator.workflow import SupportWorkflow
from ticketideai.schemas.store import SupportRequest
from ticketideai.schemas.workflow import WorkflowResult
from ticketideai.tools.sandbox import SandboxExecutor
from ticketideai.tools.sandbox_runner import SandboxLimits
from ticketideai.tools.seed_store import SEED_DATE

FAST_LIMITS = SandboxLimits(wall_seconds=4.0, cpu_seconds=2, rss_mb=200, output_bytes=200_000)
CUSTOMER_1 = "customer1@example.com"
CUSTOMER_11 = "customer11@example.com"


def plan_source(*lines: str) -> str:
    """Build a plan from body lines, ending with a `done` result."""
    body = "\n".join(f"    {line}" for line in lines)
    return f'def plan(api):\n{body}\n    return {{"outcome": "done", "summary": "ok"}}\n'


class FakeProvider:
    """Return queued structured outputs; the last one repeats."""

    def __init__(self, outputs: list[Any]) -> None:
        self.model = "fake-model"
        self.tracer = None
        self.outputs = list(outputs)
        self.requests: list[str] = []

    def structured(self, messages: list[Any], schema: type[BaseModel], **_: Any) -> BaseModel:
        self.requests.append(messages[-1].content)
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(item, Exception):
            raise item
        return schema(**item)


class FakeRouter:
    """Serve scripted planner plans and responder replies."""

    def __init__(self, sources: list[str], replies: list[Any] | None = None) -> None:
        plans = [{"summary": "test plan", "source": source} for source in sources]
        self.tracer = None
        self.planner = FakeProvider(plans)
        self.responder = FakeProvider(replies or [{"body": "Thanks for contacting us."}])

    def for_role(self, role: str) -> FakeProvider:
        return {"planner": self.planner, "responder": self.responder}[role]


def build_workflow(
    database_url: str,
    sources: list[str],
    *,
    replies: list[Any] | None = None,
    sandbox: SandboxExecutor | None = None,
    as_of: datetime = SEED_DATE,
) -> SupportWorkflow:
    """Create a workflow with scripted models and the seed date as today."""
    return SupportWorkflow(
        FakeRouter(sources, replies),  # type: ignore[arg-type]
        database_url,
        sandbox=sandbox or SandboxExecutor(FAST_LIMITS),
        clock=lambda: as_of,
    )


def handle(
    database_url: str, source: str, *, email: str = CUSTOMER_1, message: str = "help"
) -> WorkflowResult:
    """Run one scripted plan through the whole workflow."""
    workflow = build_workflow(database_url, [source])
    return workflow.handle(SupportRequest(customer_email=email, message=message))

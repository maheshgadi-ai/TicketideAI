"""Pydantic schemas for deterministic policy-conformance fixtures."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ticketideai.schemas.workflow import Outcome


class Scenario(BaseModel):
    """A fixed plan and the outcome store policy must produce for it."""

    id: str
    message: str
    customer_email: str
    plan_source: str
    expected_outcome: Outcome


class ScenarioResult(BaseModel):
    """How one fixture was actually resolved."""

    scenario_id: str
    task_success: bool
    outcome: Outcome
    expected_outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    policy_violation: bool
    escalation_correct: bool
    latency_ms: float


class EvalReport(BaseModel):
    """Aggregate results for the fixture suite."""

    scenario_count: int
    success_count: int
    task_success_rate: float
    policy_violation_count: int
    escalation_accuracy: float
    average_latency_ms: float
    results: list[ScenarioResult] = Field(default_factory=list)

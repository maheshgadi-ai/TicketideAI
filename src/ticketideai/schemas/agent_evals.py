"""Pydantic schemas for the end-to-end agent evaluation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ticketideai.schemas.workflow import Outcome, ReplyFacts

FaultKind = Literal[
    "persistent_contention",
    "single_concurrent_change",
    "transient_database_error",
    "sandbox_limit",
]


class ReturnEffect(BaseModel):
    """Units of one SKU returned on one order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order: int
    sku: str
    quantity: int


class StateEffects(BaseModel):
    """Store changes in a form that can be compared regardless of operation order."""

    model_config = ConfigDict(extra="forbid")

    returns: list[ReturnEffect] = Field(default_factory=list)
    refunds: dict[int, float] = Field(default_factory=dict)
    stock: dict[str, int] = Field(default_factory=dict)
    status: dict[int, str] = Field(default_factory=dict)

    def normalized(self) -> tuple[Any, ...]:
        """Return a canonical value: totals per key, with zero entries removed."""
        returned: dict[tuple[int, str], int] = {}
        for item in self.returns:
            key = (item.order, item.sku)
            returned[key] = returned.get(key, 0) + item.quantity
        refunds = {order: round(value * 100) for order, value in self.refunds.items() if value}
        stock = {sku: delta for sku, delta in self.stock.items() if delta}
        return (
            sorted(returned.items()),
            sorted(refunds.items()),
            sorted(stock.items()),
            sorted(self.status.items()),
        )

    @property
    def is_empty(self) -> bool:
        """True when no store change is described."""
        return self.normalized() == ([], [], [], [])


class AgentScenario(BaseModel):
    """A customer message, the correct outcome, and the correct store effects."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    customer_email: str
    message: str
    expected_outcome: Outcome
    effects: StateEffects = Field(default_factory=StateEffects)
    also_accepted: list[Literal["refused", "needs_clarification", "failed_safely"]] = Field(
        default_factory=list
    )
    protected_orders: list[int] = Field(default_factory=list)
    fault: FaultKind | None = None

    def accepts(self, outcome: Outcome) -> bool:
        """Return True when `outcome` counts as correct for this scenario."""
        return outcome == self.expected_outcome or outcome in self.also_accepted


class JudgeInput(BaseModel):
    """Evidence and the reply being checked."""

    outcome: Outcome
    facts: ReplyFacts
    change_set: str
    lookup_results: dict[str, Any] = Field(default_factory=dict)
    reply: str


class JudgeVerdict(BaseModel):
    """Whether a reply is supported by the evidence."""

    grounded: bool
    unsupported_claims: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """One scenario run under one model profile."""

    profile: str
    scenario_id: str
    category: str
    repeat: int
    outcome: Outcome | None
    expected_outcome: Outcome
    outcome_correct: bool
    state_correct: bool
    task_success: bool
    violations: list[str] = Field(default_factory=list)
    disclosures: list[str] = Field(default_factory=list)
    fault: FaultKind | None = None
    fault_fired: bool | None = None
    reply_honest: bool = True
    precondition_aborts: int = 0
    transient_errors: int = 0
    sandbox_limit_failures: int = 0
    reflections: int = 0
    attempt_errors: list[str] = Field(default_factory=list)
    failed_plans: list[str] = Field(default_factory=list)
    guard_replaced: bool = False
    guard_problems: list[str] = Field(default_factory=list)
    reply_draft: str = ""
    grounded: bool | None = None
    unsupported_claims: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    judge_cost_usd: float = 0.0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_by_role: dict[str, dict[str, float]] = Field(default_factory=dict)
    reply: str = ""
    error: str | None = None


class FaultSummary(BaseModel):
    """Results for one injected fault, kept apart from the core agent metrics."""

    runs: int
    successes: int
    fault_fired: int
    outcomes: dict[str, int]
    runs_with_reflection: int
    reflections: int
    precondition_aborts: int
    transient_errors: int
    sandbox_limit_failures: int
    honest_replies: int


class ProfileSummary(BaseModel):
    """Aggregate metrics for one model profile."""

    profile: str
    models: dict[str, str]
    runs: int
    task_successes: int
    task_success_rate: float
    task_success_ci95: tuple[float, float]
    core_runs: int
    core_task_successes: int
    core_task_success_rate: float
    core_task_success_ci95: tuple[float, float]
    fault_results: dict[str, FaultSummary]
    escalation_accuracy: float
    unnecessary_escalations: int
    missed_escalations: int
    policy_violations_committed: int
    violation_rate_upper_bound95: float
    runs_with_reflection: int
    mean_reflections: float
    replies_judged: int
    groundedness_rate: float | None
    guard_replacements: int
    replies_disclosing_protected_data: int
    errors: int
    agent_cost_usd: float
    judge_cost_usd: float
    mean_agent_cost_usd: float
    latency_mean_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    success_by_category: dict[str, str]
    success_by_scenario: dict[str, str]


class AgentEvalReport(BaseModel):
    """Full agent evaluation output."""

    scenario_count: int
    repeats: int
    stopped_early: bool = False
    profiles: list[ProfileSummary]
    runs: list[AgentRunResult]

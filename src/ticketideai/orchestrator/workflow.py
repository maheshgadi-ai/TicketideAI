"""Plan, dry-run, check policy, then commit, escalate, or refuse a support request."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.exc import OperationalError

from ticketideai.agents.planner import PlannerAgent
from ticketideai.agents.responder import ResponderAgent
from ticketideai.llm import ModelRouter
from ticketideai.observability.tracing import Tracer
from ticketideai.orchestrator.reply_facts import build_reply_facts
from ticketideai.schemas.operations import ChangeSet
from ticketideai.schemas.store import PlanRequest, PlanResponse, SupportRequest
from ticketideai.schemas.workflow import (
    STATE_CHANGED_ERROR,
    TRANSIENT_STORE_ERROR,
    PolicyDecision,
    WorkflowResult,
)
from ticketideai.tools.approval_queue import ApprovalQueue
from ticketideai.tools.diffing import render_change_set
from ticketideai.tools.effects import commit_change_set
from ticketideai.tools.errors import PreconditionFailed, StoreError
from ticketideai.tools.policy import PolicyEngine
from ticketideai.tools.sandbox import SandboxExecutor, SandboxResult
from ticketideai.tools.store_api import StoreAPI
from ticketideai.tools.store_context import build_store_context

MAX_REFLECTIONS = 2
NOT_COMPLETED_REASON = "We could not complete this safely, and nothing on your order was changed."
UNKNOWN_ACCOUNT_QUESTION = "We could not find an account for this email. Which email did you use?"


class SupportWorkflow:
    """Handle one request. Model-written code only ever runs inside the sandbox."""

    def __init__(
        self,
        router: ModelRouter,
        database_url: str,
        *,
        policy_engine: PolicyEngine | None = None,
        sandbox: SandboxExecutor | None = None,
        approval_queue: ApprovalQueue | None = None,
        tracer: Tracer | None = None,
        max_reflections: int = MAX_REFLECTIONS,
        clock: Callable[[], datetime] = datetime.now,
        committer: Callable[[str, ChangeSet], None] = commit_change_set,
    ) -> None:
        self.database_url = database_url
        self.committer = committer
        self.policy_engine = policy_engine or PolicyEngine.from_file()
        self.sandbox = sandbox or SandboxExecutor()
        self.approval_queue = approval_queue or ApprovalQueue(database_url)
        self.approval_queue.initialize()
        self.tracer = tracer or router.tracer or Tracer()
        self.max_reflections = max_reflections
        self.clock = clock
        self.planner = PlannerAgent(router.for_role("planner"), self.tracer)
        self.responder = ResponderAgent(router.for_role("responder"), self.tracer)

    def handle(self, request: SupportRequest) -> WorkflowResult:
        """Run the full workflow and return the outcome with the customer reply."""
        with self.tracer.observe(
            "workflow.handle", as_type="chain", input=request.model_dump()
        ) as observation:
            result = self._decide(request)
            facts = build_reply_facts(request.message, result)
            response = self.responder.respond(facts)
            final = result.model_copy(
                update={
                    "reply": response.reply,
                    "reply_replaced_by_guard": response.replaced,
                    "reply_draft": response.draft,
                    "guard_problems": response.problems,
                }
            )
            observation.update(
                output={"outcome": final.outcome, "operations": len(final.change_set.operations)}
            )
            return final

    def _decide(self, request: SupportRequest) -> WorkflowResult:
        requester_id = self._requester_id(request.customer_email)
        if requester_id is None:
            return WorkflowResult(
                outcome="needs_clarification", plan_result={"question": UNKNOWN_ACCOUNT_QUESTION}
            )
        errors: list[str] = []
        failed_plans: list[str] = []
        source: str | None = None
        for attempt in range(self.max_reflections + 1):
            plan = self._plan(request, source, errors[-1] if errors else None, attempt)
            source = plan.source
            run = self._dry_run(plan.source, request.customer_email)
            if not run.success:
                errors.append(run.error)
                failed_plans.append(plan.source)
                continue
            result = self._resolve(request, plan, run, requester_id, attempt, errors)
            if result is not None:
                return result.model_copy(update={"failed_plans": failed_plans})
            failed_plans.append(plan.source)
        return WorkflowResult(
            outcome="failed_safely",
            plan_source=source or "",
            plan_result={"reason": NOT_COMPLETED_REASON},
            reflection_attempts=self.max_reflections,
            errors=errors,
            failed_plans=failed_plans,
        )

    def _resolve(
        self,
        request: SupportRequest,
        plan: PlanResponse,
        run: SandboxResult,
        requester_id: int,
        attempt: int,
        errors: list[str],
    ) -> WorkflowResult | None:
        """Return the final result, or None when the store changed and a re-plan is needed."""
        base = WorkflowResult(
            outcome="refused",
            plan_summary=plan.summary,
            plan_source=plan.source,
            plan_result=run.result,
            reflection_attempts=attempt,
            errors=list(errors),
        )
        declared = run.result.get("outcome")
        if declared == "clarify":
            return base.model_copy(update={"outcome": "needs_clarification"})
        if declared == "refuse":
            return base
        decision = self._check_policy(run, requester_id)
        reviewed = base.model_copy(
            update={
                "policy_action": decision.action,
                "findings": decision.findings,
                "change_set": run.change_set,
                "diff": run.diff,
                "diff_text": render_change_set(run.change_set),
            }
        )
        if decision.action == "block":
            return reviewed
        if decision.action == "escalate":
            return self._escalate(request, reviewed, decision)
        if run.change_set.is_empty:
            return reviewed.model_copy(update={"outcome": "answered"})
        return self._commit(reviewed, errors)

    def _commit(self, reviewed: WorkflowResult, errors: list[str]) -> WorkflowResult | None:
        with self.tracer.observe("workflow.commit", as_type="tool") as observation:
            try:
                self.committer(self.database_url, reviewed.change_set)
            except PreconditionFailed as exc:
                errors.append(f"{STATE_CHANGED_ERROR}: {exc}")
                observation.update(level="WARNING", status_message=errors[-1])
                return None
            except OperationalError:
                errors.append(TRANSIENT_STORE_ERROR)
                observation.update(level="WARNING", status_message=errors[-1])
                return None
        return reviewed.model_copy(update={"outcome": "committed"})

    def _escalate(
        self, request: SupportRequest, reviewed: WorkflowResult, decision: PolicyDecision
    ) -> WorkflowResult:
        item = self.approval_queue.enqueue(
            customer_email=request.customer_email,
            message=request.message,
            plan_source=reviewed.plan_source,
            reasons=decision.reasons,
            change_set=reviewed.change_set,
            diff=reviewed.diff,
            diff_text=reviewed.diff_text,
        )
        return reviewed.model_copy(update={"outcome": "escalated", "approval_id": item.id})

    def _plan(
        self, request: SupportRequest, source: str | None, error: str | None, attempt: int
    ) -> PlanResponse:
        context = build_store_context(self.database_url, request.customer_email, self.clock())
        return self.planner.plan(
            PlanRequest(
                customer_email=request.customer_email,
                message=request.message,
                store_context=context,
                previous_source=source,
                reflection_error=error,
                reflection_attempt=attempt,
            )
        )

    def _dry_run(self, source: str, customer_email: str) -> SandboxResult:
        with self.tracer.observe("sandbox.dry_run", as_type="tool", input=source) as observation:
            run = self.sandbox.execute(source, self.database_url, customer_email)
            observation.update(output={"success": run.success, "error": run.error})
            return run

    def _check_policy(self, run: SandboxResult, requester_id: int) -> PolicyDecision:
        with self.tracer.observe("policy.evaluate", as_type="tool") as observation:
            decision = self.policy_engine.evaluate(
                run.change_set, requester_id=requester_id, as_of=self.clock()
            )
            observation.update(output=decision.model_dump())
            return decision

    def _requester_id(self, customer_email: str) -> int | None:
        try:
            return StoreAPI(self.database_url).find_customer(customer_email).id
        except StoreError:
            return None

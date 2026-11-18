"""Run customer-message scenarios through the full agent on scratch databases."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ticketideai.agents.judge import GroundednessJudge
from ticketideai.evals.agent_audit import audit_run
from ticketideai.evals.agent_faults import FaultHooks, build_fault
from ticketideai.evals.agent_leak import find_disclosures
from ticketideai.evals.agent_state import effects_from_snapshots, state_matches
from ticketideai.llm import LLMError
from ticketideai.orchestrator.reply_facts import build_reply_facts
from ticketideai.orchestrator.workflow import SupportWorkflow
from ticketideai.schemas.agent_evals import AgentRunResult, AgentScenario, JudgeInput
from ticketideai.schemas.store import SupportRequest
from ticketideai.schemas.workflow import (
    STATE_CHANGED_ERROR,
    TRANSIENT_STORE_ERROR,
    WorkflowResult,
)
from ticketideai.tools.approval_queue import ApprovalQueue
from ticketideai.tools.diffing import snapshot
from ticketideai.tools.policy import load_policy
from ticketideai.tools.reply_guard import reply_problems
from ticketideai.tools.seed_store import SEED_DATE, generate_store
from ticketideai.tools.store_api import StoreAPI
from ticketideai.tools.store_context import build_store_context

logger = logging.getLogger(__name__)
RouterFactory = Callable[[str], Any]
RunKey = tuple[str, str, int]


class AgentEvalRunner:
    """Execute every scenario, profile, and repeat, each on its own new database."""

    def __init__(
        self,
        scenarios: list[AgentScenario],
        router_factory: RouterFactory,
        *,
        workers: int = 1,
        max_cost_usd: float | None = None,
        on_result: Callable[[AgentRunResult], None] | None = None,
    ) -> None:
        self.scenarios = scenarios
        self.router_factory = router_factory
        self.workers = workers
        self.max_cost_usd = max_cost_usd
        self.on_result = on_result
        self.policy = load_policy()
        self.spent_usd = 0.0
        self.stopped_early = False
        self._lock = threading.Lock()

    def run(
        self, profiles: Iterable[str], repeats: int, completed: set[RunKey] | None = None
    ) -> list[AgentRunResult]:
        """Run all pending combinations, stopping new runs at the spending cap.

        Each repeat finishes on every profile before the next begins, so a cap stop still
        leaves the profiles with comparable numbers of completed repeats.
        """
        done = completed or set()
        profile_list = list(profiles)
        jobs = [
            (profile, scenario, repeat)
            for repeat in range(1, repeats + 1)
            for profile in profile_list
            for scenario in self.scenarios
            if (profile, scenario.id, repeat) not in done
        ]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda job: self._guarded_run(*job), jobs))
        return [result for result in results if result is not None]

    def _guarded_run(
        self, profile: str, scenario: AgentScenario, repeat: int
    ) -> AgentRunResult | None:
        with self._lock:
            if self.max_cost_usd is not None and self.spent_usd >= self.max_cost_usd:
                self.stopped_early = True
                return None
        result = self.run_one(profile, scenario, repeat)
        with self._lock:
            self.spent_usd += result.cost_usd + result.judge_cost_usd
            if self.on_result:
                self.on_result(result)
        return result

    def run_one(self, profile: str, scenario: AgentScenario, repeat: int) -> AgentRunResult:
        """Run one scenario on a new scratch database."""
        router = self.router_factory(profile)
        with tempfile.TemporaryDirectory(prefix="ticketide-agent-eval-") as work:
            database = Path(work) / "store.sqlite"
            generate_store(database)
            url = f"sqlite:///{database}"
            before = snapshot(database)
            store_records = build_store_context(url, scenario.customer_email, SEED_DATE)
            hooks = build_fault(scenario.fault, database)
            workflow = SupportWorkflow(
                router,
                url,
                clock=lambda: SEED_DATE,
                sandbox=hooks.sandbox,
                committer=hooks.committer,
            )
            request = SupportRequest(
                customer_email=scenario.customer_email, message=scenario.message
            )
            started = time.perf_counter()
            try:
                outcome = workflow.handle(request)
            except LLMError as exc:
                logger.warning("agent eval run failed", extra={"scenario": scenario.id})
                return _failed_run(profile, scenario, repeat, str(exc), router)
            latency = (time.perf_counter() - started) * 1000
            after = snapshot(database)
            pending = [item.change_set for item in ApprovalQueue(url).list_pending()]
            requester = StoreAPI(url).find_customer(scenario.customer_email).id
        baseline = hooks.baseline or before
        committed = effects_from_snapshots(baseline, after)
        return self._score(
            profile,
            scenario,
            repeat,
            router,
            outcome,
            latency,
            state_matches(scenario, committed, pending, outcome.outcome),
            audit_run(baseline, after, requester, SEED_DATE, self.policy),
            find_disclosures(outcome.reply, before, scenario.protected_orders, requester),
            hooks,
            store_records,
        )

    def _score(
        self,
        profile: str,
        scenario: AgentScenario,
        repeat: int,
        router: Any,
        outcome: WorkflowResult,
        latency: float,
        state_correct: bool,
        violations: list[str],
        disclosures: list[str],
        hooks: FaultHooks,
        store_records: dict[str, Any],
    ) -> AgentRunResult:
        agent_usage = _usage_snapshot(router)
        grounded, claims = _judge_reply(router, scenario, outcome, store_records)
        judge_cost = router.usage.cost_usd - agent_usage["cost"]
        correct = scenario.accepts(outcome.outcome)
        facts = build_reply_facts(scenario.message, outcome)
        honest = not reply_problems(outcome.reply, facts)
        fired = hooks.fired if scenario.fault else None
        fault_passed = scenario.fault is None or (bool(fired) and honest)
        return AgentRunResult(
            profile=profile,
            scenario_id=scenario.id,
            category=scenario.category,
            repeat=repeat,
            outcome=outcome.outcome,
            expected_outcome=scenario.expected_outcome,
            outcome_correct=correct,
            state_correct=state_correct,
            task_success=correct and state_correct and not disclosures and fault_passed,
            violations=violations,
            disclosures=disclosures,
            fault=scenario.fault,
            fault_fired=fired,
            reply_honest=honest,
            precondition_aborts=_count(outcome.errors, STATE_CHANGED_ERROR),
            transient_errors=_count(outcome.errors, TRANSIENT_STORE_ERROR),
            sandbox_limit_failures=_count(outcome.errors, "plan exceeded the"),
            reflections=outcome.reflection_attempts,
            attempt_errors=outcome.errors,
            failed_plans=outcome.failed_plans,
            guard_replaced=outcome.reply_replaced_by_guard,
            guard_problems=outcome.guard_problems,
            reply_draft=outcome.reply_draft,
            grounded=grounded,
            unsupported_claims=claims,
            cost_usd=agent_usage["cost"],
            judge_cost_usd=judge_cost,
            latency_ms=latency,
            input_tokens=agent_usage["input"],
            output_tokens=agent_usage["output"],
            usage_by_role=_usage_by_role(router),
            reply=outcome.reply,
        )


def _judge_reply(
    router: Any, scenario: AgentScenario, outcome: WorkflowResult, store_records: dict[str, Any]
) -> tuple[bool | None, list[str]]:
    evidence = JudgeInput(
        outcome=outcome.outcome,
        facts=build_reply_facts(scenario.message, outcome),
        change_set=outcome.diff_text,
        lookup_results={"store_records": store_records, "plan_results": outcome.plan_result},
        reply=outcome.reply,
    )
    try:
        verdict = GroundednessJudge(router.for_role("judge"), router.tracer).judge(evidence)
    except LLMError:
        logger.warning("judge call failed", extra={"scenario": scenario.id})
        return None, []
    return verdict.grounded, verdict.unsupported_claims


def _usage_snapshot(router: Any) -> dict[str, Any]:
    usage = router.usage
    return {"cost": usage.cost_usd, "input": usage.input_tokens, "output": usage.output_tokens}


def _count(errors: list[str], prefix: str) -> int:
    return sum(error.startswith(prefix) for error in errors)


def _usage_by_role(router: Any) -> dict[str, dict[str, float]]:
    return {
        role: {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cost_usd": usage.cost_usd,
        }
        for role, usage in router.usage.by_role.items()
    }


def _failed_run(
    profile: str, scenario: AgentScenario, repeat: int, error: str, router: Any
) -> AgentRunResult:
    usage = _usage_snapshot(router)
    return AgentRunResult(
        profile=profile,
        scenario_id=scenario.id,
        category=scenario.category,
        repeat=repeat,
        outcome=None,
        expected_outcome=scenario.expected_outcome,
        outcome_correct=False,
        state_correct=False,
        task_success=False,
        fault=scenario.fault,
        cost_usd=usage["cost"],
        input_tokens=usage["input"],
        output_tokens=usage["output"],
        usage_by_role=_usage_by_role(router),
        error=error,
    )

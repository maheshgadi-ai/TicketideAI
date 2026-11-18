"""Run fixed plans through the real sandbox and policy engine. No LLM is involved."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ticketideai.evals.dataset import load_scenarios
from ticketideai.schemas.evals import EvalReport, Scenario, ScenarioResult
from ticketideai.schemas.workflow import Outcome
from ticketideai.tools.effects import commit_change_set
from ticketideai.tools.errors import StoreError
from ticketideai.tools.policy import DEFAULT_POLICY_PATH, PolicyEngine
from ticketideai.tools.sandbox import SandboxExecutor
from ticketideai.tools.seed_store import SEED_DATE, generate_store
from ticketideai.tools.store_api import StoreAPI


class ScenarioRunner:
    """Score each fixture on a fresh scratch copy of the seeded store."""

    def __init__(self, policy_path: str | Path = DEFAULT_POLICY_PATH) -> None:
        self.policy = PolicyEngine.from_file(policy_path)
        self.sandbox = SandboxExecutor()

    def run(self, dataset_path: str | Path) -> EvalReport:
        """Run every fixture and return the aggregate report."""
        results = [self._run_scenario(item) for item in load_scenarios(dataset_path)]
        count = len(results)
        successes = sum(result.task_success for result in results)
        return EvalReport(
            scenario_count=count,
            success_count=successes,
            task_success_rate=successes / count if count else 0.0,
            policy_violation_count=sum(result.policy_violation for result in results),
            escalation_accuracy=(
                sum(result.escalation_correct for result in results) / count if count else 0.0
            ),
            average_latency_ms=sum(r.latency_ms for r in results) / count if count else 0.0,
            results=results,
        )

    def _run_scenario(self, scenario: Scenario) -> ScenarioResult:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="ticketide-eval-") as work:
            database = Path(work) / "store.sqlite"
            generate_store(database)
            outcome, reasons = self._resolve(scenario, f"sqlite:///{database}")
        expected = scenario.expected_outcome
        return ScenarioResult(
            scenario_id=scenario.id,
            task_success=outcome == expected,
            outcome=outcome,
            expected_outcome=expected,
            reasons=reasons,
            policy_violation=outcome == "committed" and expected != "committed",
            escalation_correct=(outcome == "escalated") == (expected == "escalated"),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _resolve(self, scenario: Scenario, database_url: str) -> tuple[Outcome, list[str]]:
        run = self.sandbox.execute(scenario.plan_source, database_url, scenario.customer_email)
        if not run.success:
            return "refused", [run.error]
        try:
            requester = StoreAPI(database_url).find_customer(scenario.customer_email)
        except StoreError as exc:
            return "refused", [str(exc)]
        decision = self.policy.evaluate(run.change_set, requester_id=requester.id, as_of=SEED_DATE)
        if decision.action == "block":
            return "refused", decision.reasons
        if decision.action == "escalate":
            return "escalated", decision.reasons
        if run.change_set.is_empty:
            return "answered", []
        commit_change_set(database_url, run.change_set)
        return "committed", []

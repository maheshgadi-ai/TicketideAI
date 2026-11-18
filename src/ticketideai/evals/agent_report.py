"""Aggregate agent-eval runs into metrics and write reports."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from ticketideai.schemas.agent_evals import (
    AgentEvalReport,
    AgentRunResult,
    FaultSummary,
    ProfileSummary,
)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Return the Wilson score interval for a proportion."""
    if total == 0:
        return (0.0, 0.0)
    rate = successes / total
    denominator = 1 + z**2 / total
    center = (rate + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z**2 / (4 * total**2)) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def percentile(values: list[float], fraction: float) -> float:
    """Return a nearest-rank percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def summarize(profile: str, models: dict[str, str], runs: list[AgentRunResult]) -> ProfileSummary:
    """Compute every reported metric for one profile."""
    total = len(runs)
    successes = sum(run.task_success for run in runs)
    core = [run for run in runs if run.fault is None]
    core_successes = sum(run.task_success for run in core)
    violating = sum(bool(run.violations) for run in runs)
    judged = [run for run in runs if run.grounded is not None]
    latencies = [run.latency_ms for run in runs if run.error is None]
    return ProfileSummary(
        profile=profile,
        models=models,
        runs=total,
        task_successes=successes,
        task_success_rate=_rate(successes, total),
        task_success_ci95=wilson_interval(successes, total),
        core_runs=len(core),
        core_task_successes=core_successes,
        core_task_success_rate=_rate(core_successes, len(core)),
        core_task_success_ci95=wilson_interval(core_successes, len(core)),
        fault_results=_fault_results(runs),
        escalation_accuracy=_rate(sum(_escalation_correct(run) for run in core), len(core)),
        unnecessary_escalations=sum(
            run.outcome == "escalated" and run.expected_outcome != "escalated" for run in core
        ),
        missed_escalations=sum(
            run.expected_outcome == "escalated" and run.outcome != "escalated" for run in core
        ),
        policy_violations_committed=sum(len(run.violations) for run in runs),
        violation_rate_upper_bound95=_violation_bound(violating, total),
        runs_with_reflection=sum(run.reflections > 0 for run in core),
        mean_reflections=_rate(sum(run.reflections for run in core), len(core)),
        replies_judged=len(judged),
        groundedness_rate=_rate(sum(bool(r.grounded) for r in judged), len(judged))
        if judged
        else None,
        guard_replacements=sum(run.guard_replaced for run in runs),
        replies_disclosing_protected_data=sum(bool(run.disclosures) for run in runs),
        errors=sum(run.error is not None for run in runs),
        agent_cost_usd=sum(run.cost_usd for run in runs),
        judge_cost_usd=sum(run.judge_cost_usd for run in runs),
        mean_agent_cost_usd=_rate(sum(run.cost_usd for run in runs), total),
        latency_mean_ms=_rate(sum(latencies), len(latencies)),
        latency_p50_ms=percentile(latencies, 0.50),
        latency_p95_ms=percentile(latencies, 0.95),
        success_by_category=_grouped(runs, "category"),
        success_by_scenario=_grouped(runs, "scenario_id"),
    )


def write_agent_report(report: AgentEvalReport, output_dir: Path) -> None:
    """Write the JSON and Markdown agent-eval reports."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "agent_eval.json").write_text(
        json.dumps(report.model_dump(), indent=2), encoding="utf-8"
    )
    (output_dir / "agent_eval.md").write_text(_markdown(report), encoding="utf-8")


def _markdown(report: AgentEvalReport) -> str:
    lines = [
        "# Agent Evaluation",
        "",
        f"{report.scenario_count} scenarios, {report.repeats} repeats per model profile.",
        "",
        "| Metric | " + " | ".join(item.profile for item in report.profiles) + " |",
        "|---|" + "---|" * len(report.profiles),
    ]
    rows = {
        "Runs, including fault injection": lambda s: str(s.runs),
        "Core task success": lambda s: (
            f"{s.core_task_successes}/{s.core_runs} ({s.core_task_success_rate:.1%})"
        ),
        "Core task success 95% CI": lambda s: (
            f"{s.core_task_success_ci95[0]:.1%} to {s.core_task_success_ci95[1]:.1%}"
        ),
        "All-run task success": lambda s: (
            f"{s.task_successes}/{s.runs} ({s.task_success_rate:.1%})"
        ),
        "Escalation accuracy": lambda s: f"{s.escalation_accuracy:.1%}",
        "Unnecessary escalations": lambda s: str(s.unnecessary_escalations),
        "Missed escalations": lambda s: str(s.missed_escalations),
        "Policy violations committed": lambda s: str(s.policy_violations_committed),
        "Violation rate 95% upper bound": lambda s: f"{s.violation_rate_upper_bound95:.1%}",
        "Core runs using reflection": lambda s: str(s.runs_with_reflection),
        "Reply groundedness": lambda s: (
            "n/a" if s.groundedness_rate is None else f"{s.groundedness_rate:.1%}"
        ),
        "Replies replaced by guard": lambda s: str(s.guard_replacements),
        "Replies disclosing protected data": lambda s: str(s.replies_disclosing_protected_data),
        "Agent cost": lambda s: f"${s.agent_cost_usd:.2f}",
        "Judge cost": lambda s: f"${s.judge_cost_usd:.2f}",
        "Latency p50 / p95": lambda s: (
            f"{s.latency_p50_ms / 1000:.1f} s / {s.latency_p95_ms / 1000:.1f} s"
        ),
        "Run errors": lambda s: str(s.errors),
    }
    for name, value in rows.items():
        lines.append(f"| {name} | " + " | ".join(value(s) for s in report.profiles) + " |")
    return "\n".join([*lines, *_fault_lines(report)]) + "\n"


def _fault_lines(report: AgentEvalReport) -> list[str]:
    lines = [
        "",
        "## Fault injection",
        "",
        "| Profile | Fault | Passed | Fired | Outcomes | Runs reflecting | Reflections | "
        "Precondition aborts | Transient errors | Limit failures | Honest replies |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for profile in report.profiles:
        for name, item in profile.fault_results.items():
            outcomes = ", ".join(f"{key}: {value}" for key, value in sorted(item.outcomes.items()))
            lines.append(
                f"| {profile.profile} | {name} | {item.successes}/{item.runs} | "
                f"{item.fault_fired}/{item.runs} | {outcomes} | {item.runs_with_reflection} | "
                f"{item.reflections} | {item.precondition_aborts} | {item.transient_errors} | "
                f"{item.sandbox_limit_failures} | {item.honest_replies}/{item.runs} |"
            )
    return lines


def _fault_results(runs: list[AgentRunResult]) -> dict[str, FaultSummary]:
    groups: dict[str, list[AgentRunResult]] = defaultdict(list)
    for run in runs:
        if run.fault:
            groups[run.fault].append(run)
    return {name: _fault_summary(items) for name, items in sorted(groups.items())}


def _fault_summary(runs: list[AgentRunResult]) -> FaultSummary:
    outcomes: dict[str, int] = defaultdict(int)
    for run in runs:
        outcomes[str(run.outcome)] += 1
    return FaultSummary(
        runs=len(runs),
        successes=sum(run.task_success for run in runs),
        fault_fired=sum(bool(run.fault_fired) for run in runs),
        outcomes=dict(outcomes),
        runs_with_reflection=sum(run.reflections > 0 for run in runs),
        reflections=sum(run.reflections for run in runs),
        precondition_aborts=sum(run.precondition_aborts for run in runs),
        transient_errors=sum(run.transient_errors for run in runs),
        sandbox_limit_failures=sum(run.sandbox_limit_failures for run in runs),
        honest_replies=sum(run.reply_honest for run in runs),
    )


def _escalation_correct(run: AgentRunResult) -> bool:
    return (run.outcome == "escalated") == (run.expected_outcome == "escalated")


def _violation_bound(violating: int, total: int) -> float:
    if total == 0:
        return 1.0
    if violating == 0:
        return 1 - 0.05 ** (1 / total)
    return wilson_interval(violating, total)[1]


def _grouped(runs: list[AgentRunResult], field: str) -> dict[str, str]:
    groups: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        groups[getattr(run, field)].append(run.task_success)
    return {key: f"{sum(values)}/{len(values)}" for key, values in sorted(groups.items())}


def _rate(part: float, whole: int) -> float:
    return part / whole if whole else 0.0

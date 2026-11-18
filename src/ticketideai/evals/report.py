"""Render fixture-suite reports as JSON and Markdown."""

from __future__ import annotations

import json
from pathlib import Path

from ticketideai.schemas.evals import EvalReport


def write_json(report: EvalReport, path: str | Path) -> None:
    """Write the full report as JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.model_dump(), indent=2), encoding="utf-8")


def write_markdown(report: EvalReport, path: str | Path) -> None:
    """Write a readable summary table."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Policy-Conformance Fixtures",
        "",
        "Fixed plans run through the real sandbox and policy engine. No LLM is involved, so",
        "these results measure the enforcement code, not the agent.",
        "",
        f"- Fixtures: {report.scenario_count}",
        f"- Matched expected outcome: {report.success_count}",
        f"- Policy violations committed: {report.policy_violation_count}",
        f"- Escalation accuracy: {report.escalation_accuracy:.1%}",
        f"- Average latency: {report.average_latency_ms:.0f} ms",
        "",
        "| Fixture | Expected | Actual | Result |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {result.scenario_id} | {result.expected_outcome} | {result.outcome} | "
        f"{'PASS' if result.task_success else 'FAIL'} |"
        for result in report.results
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

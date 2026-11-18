"""Deterministic policy-conformance fixture tests."""

from __future__ import annotations

from pathlib import Path

from ticketideai.evals.dataset import load_scenarios
from ticketideai.evals.report import write_json, write_markdown
from ticketideai.evals.runner import ScenarioRunner

DATASET = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "scenarios.yaml"


def test_dataset_has_twenty_unique_scenarios() -> None:
    scenarios = load_scenarios(DATASET)
    assert len({scenario.id for scenario in scenarios}) == 20


def test_every_fixture_matches_its_expected_outcome(tmp_path: Path) -> None:
    report = ScenarioRunner().run(DATASET)
    failures = [
        (result.scenario_id, result.outcome, result.reasons)
        for result in report.results
        if not result.task_success
    ]
    assert failures == []
    assert report.policy_violation_count == 0
    assert report.escalation_accuracy == 1.0
    write_json(report, tmp_path / "report.json")
    write_markdown(report, tmp_path / "report.md")
    assert "PASS" in (tmp_path / "report.md").read_text()


def test_read_only_lookups_are_not_scored_as_commits() -> None:
    report = ScenarioRunner().run(DATASET)
    outcomes = {result.scenario_id: result.outcome for result in report.results}
    assert outcomes["lookup_order"] == "answered"
    assert outcomes["stock_lookup"] == "answered"

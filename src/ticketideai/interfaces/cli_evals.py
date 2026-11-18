"""Evaluation commands for the TicketideAI command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ticketideai.config import Settings, load_models_config
from ticketideai.config.models import PROFILE_PATHS
from ticketideai.evals.agent_estimate import estimate_cost
from ticketideai.evals.agent_report import summarize, write_agent_report
from ticketideai.evals.agent_runner import AgentEvalRunner, RunKey
from ticketideai.evals.dataset import load_agent_scenarios
from ticketideai.evals.report import write_json, write_markdown
from ticketideai.evals.runner import ScenarioRunner
from ticketideai.llm import ModelRouter
from ticketideai.observability.tracing import Tracer, build_tracer
from ticketideai.schemas.agent_evals import AgentEvalReport, AgentRunResult

AGENT_DATASET = Path("evals/datasets/agent_scenarios.yaml")
RESULTS_DIR = Path("evals/results")
RERUN_OPTION = typer.Option([], "--rerun", help="With --resume, run this scenario again.")


def run_eval(
    dataset: Path = Path("evals/datasets/scenarios.yaml"),
    output_dir: Path = RESULTS_DIR,
) -> None:
    """Run the deterministic policy-conformance fixtures."""
    report = ScenarioRunner().run(dataset)
    write_json(report, output_dir / "evaluation.json")
    write_markdown(report, output_dir / "evaluation.md")
    typer.echo(
        f"Matched: {report.success_count}/{report.scenario_count}; "
        f"policy violations committed: {report.policy_violation_count}"
    )


def agent_eval_estimate(
    dataset: Path = AGENT_DATASET,
    repeats: int = typer.Option(3, min=1),
) -> None:
    """Estimate agent-eval cost from the real prompts. Makes no model calls."""
    scenarios = load_agent_scenarios(dataset)
    for profile in PROFILE_PATHS:
        item = estimate_cost(scenarios, profile, repeats)
        typer.echo(
            f"{item.profile}: {item.runs} runs; low ${item.low_usd:.2f}, "
            f"expected ${item.expected_usd:.2f}, high ${item.high_usd:.2f}, "
            f"ceiling ${item.ceiling_usd:.2f}"
        )


def agent_eval(
    dataset: Path = AGENT_DATASET,
    output_dir: Path = RESULTS_DIR,
    profile: str = typer.Option("both", help="default, budget, or both."),
    repeats: int = typer.Option(3, min=1),
    workers: int = typer.Option(4, min=1, max=8),
    max_cost_usd: float = typer.Option(..., "--max-cost-usd", help="Stop starting new runs here."),
    resume: bool = typer.Option(False, help="Keep runs already saved in the results log."),
    rerun: list[str] = RERUN_OPTION,
    confirm_billed: bool = typer.Option(False, "--confirm-billed", help="Allow paid API calls."),
) -> None:
    """Run the billed agent evaluation on scratch databases."""
    if not confirm_billed:
        typer.echo("This makes paid API calls. Re-run with --confirm-billed.", err=True)
        raise typer.Exit(code=2)
    profiles = list(PROFILE_PATHS) if profile == "both" else [profile]
    if unknown := set(profiles) - set(PROFILE_PATHS):
        raise typer.BadParameter(f"unknown profile: {sorted(unknown)}")
    scenarios = load_agent_scenarios(dataset)
    settings = Settings()
    tracer = build_tracer(settings)
    log_path = output_dir / "agent_eval_runs.jsonl"
    saved = _kept_runs(log_path, {item.id for item in scenarios}, set(rerun)) if resume else []
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(run.model_dump_json() + "\n" for run in saved), encoding="utf-8")
    runner = AgentEvalRunner(
        scenarios,
        lambda name: _profile_router(settings, name, tracer),
        workers=workers,
        max_cost_usd=max_cost_usd,
        on_result=lambda run: _append_run(log_path, run),
    )
    done: set[RunKey] = {(run.profile, run.scenario_id, run.repeat) for run in saved}
    runs = [*saved, *runner.run(profiles, repeats, done)]
    tracer.flush()
    summaries = [
        summarize(name, _profile_models(settings, name), [r for r in runs if r.profile == name])
        for name in profiles
    ]
    report = AgentEvalReport(
        scenario_count=len(scenarios),
        repeats=repeats,
        stopped_early=runner.stopped_early,
        profiles=summaries,
        runs=runs,
    )
    write_agent_report(report, output_dir)
    typer.echo(f"Completed {len(runs) - len(saved)} new runs for ${runner.spent_usd:.2f}.")


def register_eval_commands(app: typer.Typer) -> None:
    """Add the evaluation commands to the main application."""
    app.command("eval")(run_eval)
    app.command("agent-eval-estimate")(agent_eval_estimate)
    app.command("agent-eval")(agent_eval)


def _profile_router(settings: Settings, profile: str, tracer: Tracer) -> ModelRouter:
    return ModelRouter(settings, load_models_config(PROFILE_PATHS[profile]), tracer=tracer)


def _profile_models(settings: Settings, profile: str) -> dict[str, str]:
    config = load_models_config(PROFILE_PATHS[profile])
    return {role: config.resolve(role, settings).label for role in config.roles}


def _kept_runs(path: Path, known: set[str], rerun: set[str]) -> list[AgentRunResult]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    keep = known - rerun
    return [AgentRunResult.model_validate(row) for row in rows if row["scenario_id"] in keep]


def _append_run(path: Path, run: AgentRunResult) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(run.model_dump_json() + "\n")

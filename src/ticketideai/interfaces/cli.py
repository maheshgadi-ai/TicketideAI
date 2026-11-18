"""TicketideAI command-line interface."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from ticketideai.config import Settings, load_models_config
from ticketideai.interfaces.cli_evals import register_eval_commands
from ticketideai.interfaces.demo import run_demo
from ticketideai.llm import ModelRouter
from ticketideai.observability.tracing import build_tracer
from ticketideai.orchestrator.approvals import ApprovalOutcome, ApprovalService
from ticketideai.orchestrator.workflow import SupportWorkflow
from ticketideai.schemas.store import ApprovalItem, SupportRequest
from ticketideai.tools.approval_queue import ApprovalError, ApprovalQueue
from ticketideai.tools.seed_store import generate_store

APP_PATH = Path(__file__).with_name("app.py")
DEFAULT_APP_HOST = "127.0.0.1"
app = typer.Typer(help="TicketideAI customer-support agent", no_args_is_help=True)
approvals = typer.Typer(help="Human review of escalated change sets", no_args_is_help=True)
app.add_typer(approvals, name="approvals")
register_eval_commands(app)


@app.command("config")
def show_config() -> None:
    """Print the model routed to each role."""
    settings = Settings()
    config = load_models_config(settings.models_config_path)
    for role in ("default", *config.roles):
        typer.echo(f"{role}: {config.resolve(role, settings).label}")


@app.command("seed")
def seed(path: Path = Path("data/ticketide.sqlite")) -> None:
    """Create the demo store, replacing any store at PATH."""
    generate_store(path)
    typer.echo(f"Seeded {path}")


@app.command("handle")
def handle(
    message: str,
    customer_email: str = typer.Option(..., "--customer-email", help="The requesting customer."),
) -> None:
    """Handle one customer message."""
    settings = Settings()
    tracer = build_tracer(settings)
    router = ModelRouter.from_settings(settings, tracer=tracer)
    workflow = SupportWorkflow(
        router,
        settings.database_url,
        tracer=tracer,
        max_reflections=settings.max_reflections,
        clock=settings.now,
    )
    result = workflow.handle(SupportRequest(customer_email=customer_email, message=message))
    tracer.flush()
    typer.echo(f"Outcome: {result.outcome}")
    if result.diff_text:
        typer.echo(f"Change set:\n{result.diff_text}")
    if result.approval_id is not None:
        typer.echo(f"Approval item: {result.approval_id}")
    typer.echo(f"Reply:\n{result.reply}")


@app.command("demo")
def demo() -> None:
    """Run scripted requests on a scratch store. Needs no API keys and makes no model calls."""
    for line in run_demo():
        typer.echo(line)


@app.command("app")
def run_app(
    host: str = typer.Option(DEFAULT_APP_HOST, help="Address to bind. Localhost by default."),
) -> None:
    """Start the Streamlit customer chat and staff panel."""
    command = [sys.executable, "-m", "streamlit", "run", str(APP_PATH)]
    subprocess.run([*command, "--server.address", host], check=True)


@approvals.command("list")
def list_approvals() -> None:
    """Show change sets waiting for review."""
    queue = ApprovalQueue(Settings().database_url)
    queue.initialize()
    pending = queue.list_pending()
    if not pending:
        typer.echo("No pending approvals.")
    for item in pending:
        _show_item(item)


@approvals.command("approve")
def approve(
    item_id: int,
    reviewer: str = typer.Option(..., "--reviewer", help="Name of the human approving this."),
) -> None:
    """Review one change set, confirm, and commit exactly what is shown."""
    service = _service()
    _show_item(_load(service, item_id))
    typer.confirm("Commit this exact change set?", abort=True)
    _finish(lambda: service.approve(item_id, reviewer))


@approvals.command("reject")
def reject(
    item_id: int,
    reviewer: str = typer.Option(..., "--reviewer", help="Name of the human rejecting this."),
) -> None:
    """Close one change set without applying it."""
    service = _service()
    _finish(lambda: service.reject(item_id, reviewer))


def _service() -> ApprovalService:
    settings = Settings()
    return ApprovalService(settings.database_url, clock=settings.now)


def _load(service: ApprovalService, item_id: int) -> ApprovalItem:
    try:
        return service.queue.get(item_id)
    except ApprovalError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _finish(action: object) -> None:
    try:
        outcome: ApprovalOutcome = action()  # type: ignore[operator]
    except ApprovalError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Item {outcome.item.id}: {outcome.item.status}. {outcome.message}")
    if outcome.item.customer_notice:
        typer.echo(f"Customer notified: {outcome.item.customer_notice}")


def _show_item(item: ApprovalItem) -> None:
    typer.echo(f"Item {item.id} for {item.customer_email}")
    typer.echo(f"Customer message: {item.message}")
    typer.echo("Why it needs review: " + "; ".join(item.reasons))
    typer.echo(f"Change set:\n{item.diff_text}\n")


if __name__ == "__main__":
    app()

"""Approval is a human-only channel, reachable from staff interfaces alone."""

from __future__ import annotations

import ast
from pathlib import Path

from typer.testing import CliRunner

from tests.support import CUSTOMER_11, handle, plan_source
from ticketideai.interfaces.cli import app
from ticketideai.tools.sandbox_runner import PlanAPI
from ticketideai.tools.store_api import StoreAPI
from ticketideai.tools.validator import API_METHODS

SRC = Path(__file__).resolve().parents[2] / "src" / "ticketideai"
LARGE_REFUND = plan_source(
    'api.create_return(11, "RIDGE-034", 1, "damaged")', "api.issue_refund(11, 250.0)"
)


def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
    return found


def test_plans_have_no_approval_operation() -> None:
    assert not any("approv" in name or "commit" in name for name in API_METHODS)
    assert set(PlanAPI.__slots__) == set(API_METHODS)
    assert not hasattr(StoreAPI, "approve")


def test_only_the_staff_interface_imports_the_approval_service() -> None:
    importers = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "ticketideai.orchestrator.approvals" in imports_of(path)
    }
    assert importers == {"interfaces/app_runtime.py", "interfaces/cli.py", "interfaces/demo.py"}


def test_workflow_cannot_commit_a_queue_item() -> None:
    source = (SRC / "orchestrator" / "workflow.py").read_text(encoding="utf-8")
    assert "commit_approved" not in source
    assert "ApprovalService" not in source


def test_cli_approval_requires_confirmation(store_url: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", store_url)
    monkeypatch.setenv("STORE_AS_OF", "2026-11-17T00:00:00")
    item = handle(store_url, LARGE_REFUND, email=CUSTOMER_11).approval_id
    declined = CliRunner().invoke(
        app, ["approvals", "approve", str(item), "--reviewer", "Dana"], input="n\n"
    )
    assert declined.exit_code != 0
    assert "Refund $250.00 on order 11" in declined.output
    accepted = CliRunner().invoke(
        app, ["approvals", "approve", str(item), "--reviewer", "Dana"], input="y\n"
    )
    assert accepted.exit_code == 0, accepted.output
    assert "approved" in accepted.output


def test_cli_rejection_closes_the_item(store_url: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", store_url)
    item = handle(store_url, LARGE_REFUND, email=CUSTOMER_11).approval_id
    result = CliRunner().invoke(app, ["approvals", "reject", str(item), "--reviewer", "Dana"])
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    empty = CliRunner().invoke(app, ["approvals", "list"])
    assert "No pending approvals" in empty.output

"""The scripted demo runs the real pipeline on a scratch store, with no API keys."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ticketideai.interfaces.cli import app
from ticketideai.interfaces.demo import DEMO_REQUESTS, run_demo

ROOT = Path(__file__).resolve().parents[2]


def test_demo_covers_each_outcome_and_the_human_approval() -> None:
    text = "\n".join(run_demo())
    for outcome in ("answered", "committed", "escalated", "refused"):
        assert f"Outcome: {outcome}" in text
    assert "Staff approval of item 1: approved" in text
    assert "Customer notified: A team member approved your request." in text
    assert "Refund of $238.50 on order 2 issued." in text


def test_demo_replies_are_not_replaced_by_the_guard() -> None:
    text = "\n".join(run_demo())
    assert all(request.reply in text for request in DEMO_REQUESTS)


def test_demo_command_runs_without_keys() -> None:
    result = CliRunner().invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "no model calls" in result.output


def bound_address(monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **_: calls.append(command))
    result = CliRunner().invoke(app, ["app", *args])
    assert result.exit_code == 0, result.output
    return calls[0][calls[0].index("--server.address") + 1]


def test_app_binds_to_localhost_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bound_address(monkeypatch) == "127.0.0.1"


def test_app_bind_address_must_be_requested_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bound_address(monkeypatch, "--host", "0.0.0.0") == "0.0.0.0"


def test_compose_publishes_the_app_on_localhost_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["app"]["ports"] == ["127.0.0.1:8501:8501"]

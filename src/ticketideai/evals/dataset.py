"""Load and validate TicketideAI scenario datasets."""

from __future__ import annotations

from pathlib import Path

import yaml

from ticketideai.schemas.agent_evals import AgentScenario
from ticketideai.schemas.evals import Scenario


def load_scenarios(path: str | Path) -> list[Scenario]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = data.get("scenarios") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("scenario dataset must contain a scenarios list")
    scenarios = [Scenario.model_validate(row) for row in rows]
    if len(scenarios) != 20:
        raise ValueError(f"expected 20 scenarios, found {len(scenarios)}")
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario IDs must be unique")
    return scenarios


def load_agent_scenarios(path: str | Path) -> list[AgentScenario]:
    """Load customer-message scenarios for the end-to-end agent evaluation."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = [AgentScenario.model_validate(row) for row in data.get("scenarios", [])]
    ids = [scenario.id for scenario in scenarios]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("agent scenario IDs must be present and unique")
    return scenarios

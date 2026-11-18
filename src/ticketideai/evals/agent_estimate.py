"""Estimate agent-eval cost from the real prompts, without calling any model."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from ticketideai.agents.judge import PROMPT_PATH as JUDGE_PROMPT
from ticketideai.agents.planner import PROMPT_PATH as PLANNER_PROMPT
from ticketideai.agents.responder import PROMPT_PATH as RESPONDER_PROMPT
from ticketideai.config.models import PROFILE_PATHS, ModelsConfig, load_models_config
from ticketideai.config.settings import Settings
from ticketideai.schemas.agent_evals import AgentScenario
from ticketideai.schemas.store import PlanRequest
from ticketideai.tools.seed_store import SEED_DATE, generate_store
from ticketideai.tools.store_context import build_store_context

CHARS_PER_TOKEN = 3.0
REPLY_EVIDENCE_TOKENS = 450
MAX_OUTPUT_TOKENS = 16_000
MAX_PLANNER_CALLS = 3
# Per-call output tokens, including reasoning: (low, expected, high).
PLANNER_OUTPUT = (500, 1_800, 5_000)
RESPONDER_OUTPUT = (80, 250, 800)
JUDGE_OUTPUT = (150, 1_200, 4_000)
PLANNER_CALLS = (1.0, 1.2, 2.0)


@dataclass(frozen=True)
class CostEstimate:
    """Estimated spend for one model profile."""

    profile: str
    runs: int
    planner_input_tokens: int
    low_usd: float
    expected_usd: float
    high_usd: float
    ceiling_usd: float


def estimate_cost(scenarios: list[AgentScenario], profile: str, repeats: int) -> CostEstimate:
    """Estimate one profile's cost across all scenarios and repeats."""
    config = load_models_config(PROFILE_PATHS[profile])
    planner_input = _mean_planner_input(scenarios)
    responder_input = _tokens(RESPONDER_PROMPT.read_text()) + REPLY_EVIDENCE_TOKENS
    lookup_tokens = planner_input - _tokens(PLANNER_PROMPT.read_text())
    judge_input = _tokens(JUDGE_PROMPT.read_text()) + REPLY_EVIDENCE_TOKENS + lookup_tokens
    runs = len(scenarios) * repeats
    prices = _RolePrices(config)
    totals = [
        prices.planner(planner_input, PLANNER_OUTPUT[index], PLANNER_CALLS[index])
        + prices.responder(responder_input, RESPONDER_OUTPUT[index])
        + prices.judge(judge_input, JUDGE_OUTPUT[index])
        for index in range(3)
    ]
    ceiling = (
        prices.planner(planner_input, MAX_OUTPUT_TOKENS, MAX_PLANNER_CALLS)
        + prices.responder(responder_input, MAX_OUTPUT_TOKENS)
        + prices.judge(judge_input, MAX_OUTPUT_TOKENS)
    )
    return CostEstimate(
        profile=profile,
        runs=runs,
        planner_input_tokens=planner_input,
        low_usd=totals[0] * runs,
        expected_usd=totals[1] * runs,
        high_usd=totals[2] * runs,
        ceiling_usd=ceiling * runs,
    )


class _RolePrices:
    def __init__(self, config: ModelsConfig) -> None:
        settings = Settings(_env_file=None)
        self._prices = {
            role: config.price_for(config.resolve(role, settings).model)
            for role in ("planner", "responder", "judge")
        }

    def planner(self, input_tokens: int, output_tokens: int, calls: float) -> float:
        return calls * self._call("planner", input_tokens, output_tokens)

    def responder(self, input_tokens: int, output_tokens: int) -> float:
        return self._call("responder", input_tokens, output_tokens)

    def judge(self, input_tokens: int, output_tokens: int) -> float:
        return self._call("judge", input_tokens, output_tokens)

    def _call(self, role: str, input_tokens: int, output_tokens: int) -> float:
        price = self._prices[role]
        return (
            input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
        ) / 1_000_000


def _mean_planner_input(scenarios: list[AgentScenario]) -> int:
    prompt = _tokens(PLANNER_PROMPT.read_text())
    with tempfile.TemporaryDirectory(prefix="ticketide-estimate-") as work:
        database = Path(work) / "store.sqlite"
        generate_store(database)
        sizes = [_request_tokens(item, f"sqlite:///{database}") for item in scenarios]
    return prompt + round(sum(sizes) / len(sizes))


def _request_tokens(scenario: AgentScenario, url: str) -> int:
    context = build_store_context(url, scenario.customer_email, SEED_DATE)
    request = PlanRequest(
        customer_email=scenario.customer_email, message=scenario.message, store_context=context
    )
    return _tokens(request.model_dump_json())


def _tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)
